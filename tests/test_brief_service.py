"""Phase 6b: daily brief store + service.

Covers:

* `store/briefs` — upsert (overwrite same-day), get, latest, list_recent.
* `services/brief.gather_context` — shape of the context handed to the
  model (indices / movers / news / upcoming CA).
* `services/brief.generate_for` — happy path, budget cap gate,
  overwrite-on-rerun, no-key degrade, dry-run, empty-reply failure.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from brvm.config import reset_settings_cache
from brvm.db import connect
from brvm.models import (
    Brief,
    IndexLevel,
    NewsItem,
    Quote,
    Security,
)
from brvm.sources._dedupe import news_hash
from brvm.store import briefs as briefs_repo
from brvm.store import news as news_repo
from brvm.store import quotes as quotes_repo
from brvm.store import securities as sec_repo
from brvm.store import spend as spend_repo

from ._fake_anthropic import FakeAnthropic, reply
from .conftest import apply_migrations


def _setup(monkeypatch, tmp_path: Path):
    """Fresh DB + tickers + one snapshot + one tagged news row."""
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from brvm.services import brief as svc
    from brvm.services import llm as llm_mod

    llm_mod.reset_client()

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
            Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
            Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
        ])
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="SNTS", source="sikafinance", last=32_500, change_pct=4.5,
                  volume=3000, turnover=97_500_000),
            Quote(ticker="ORAC", source="sikafinance", last=19_000, change_pct=-3.2,
                  volume=1000, turnover=19_000_000),
        ])
        quotes_repo.upsert_index_levels(conn, [
            IndexLevel(ticker="BRVMC", session_date=date(2026, 8, 20),
                       level=307.42, change_pct=0.53, source="sikafinance"),
        ])
        _seed_news(conn, title="SONATEL H1 results",
                   published_at="2026-08-20T09:00:00Z", tickers="SNTS",
                   relevance=8, category="earnings")
    return db_path, svc


def _seed_news(conn, *, title, published_at, tickers, relevance, category):
    url = f"https://x/{title.replace(' ', '_')}"
    news_repo.upsert_news_items(conn, [NewsItem(
        source="sikafinance", kind="news", url=url,
        url_hash=news_hash(url, title), title=title,
        published_at=published_at, ticker_hint=None,
    )])
    row_id = int(conn.execute(
        "SELECT id FROM news_items ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"])
    news_repo.apply_tags(
        conn, row_id,
        tickers=[t.strip() for t in tickers.split(",") if t.strip()],
        relevance=relevance, category=category,
        summary_en=f"EN: {title}", summary_fr=f"FR: {title}",
    )


# --- store -----------------------------------------------------------------


def _mk_brief(day: str, markdown: str = "# hi\nbody") -> Brief:
    return Brief(
        day=day, model="test-model", title="hi", markdown=markdown,
        context_json="{}", input_tokens=100, output_tokens=50,
        usd_micros=1000, generated_utc="2026-08-20T12:00:00Z",
        session_date=day,
    )


def test_upsert_then_get(monkeypatch, tmp_path):
    db_path, _svc = _setup(monkeypatch, tmp_path)
    with connect(db_path) as conn:
        briefs_repo.upsert(conn, _mk_brief("2026-08-20"))
        got = briefs_repo.get(conn, "2026-08-20")
    assert got is not None
    assert got.title == "hi"


def test_upsert_overwrites_same_day(monkeypatch, tmp_path):
    db_path, _svc = _setup(monkeypatch, tmp_path)
    with connect(db_path) as conn:
        briefs_repo.upsert(conn, _mk_brief("2026-08-20", markdown="first"))
        briefs_repo.upsert(conn, _mk_brief("2026-08-20", markdown="second"))
        got = briefs_repo.get(conn, "2026-08-20")
        n = briefs_repo.count(conn)
    assert got.markdown == "second"
    assert n == 1


def test_latest_and_list_recent(monkeypatch, tmp_path):
    db_path, _svc = _setup(monkeypatch, tmp_path)
    with connect(db_path) as conn:
        for d in ("2026-08-19", "2026-08-20", "2026-08-21"):
            briefs_repo.upsert(conn, _mk_brief(d))
        assert briefs_repo.latest(conn).day == "2026-08-21"
        assert [b.day for b in briefs_repo.list_recent(conn, limit=2)] == [
            "2026-08-21", "2026-08-20"
        ]


# --- gather_context --------------------------------------------------------


def test_gather_context_shape(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    ctx = svc.gather_context(date(2026, 8, 20))
    assert ctx["day"] == "2026-08-20"
    # Movers include SNTS (+4.5%) and ORAC (-3.2%).
    tickers = {r["ticker"] for r in ctx["gainers"] + ctx["losers"]}
    assert {"SNTS", "ORAC"}.issubset(tickers)
    # Indices carry BRVMC.
    assert any(t["ticker"] == "BRVMC" for t in ctx["indices"])
    # The one tagged news item (relevance 8, published on the day) is in.
    assert len(ctx["news"]) == 1
    assert ctx["news"][0]["title"] == "SONATEL H1 results"


def test_gather_context_min_relevance_gate(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    # A low-relevance item on the same day must be dropped.
    with connect(db_path) as conn:
        _seed_news(conn, title="minor rumour",
                   published_at="2026-08-20T10:00:00Z",
                   tickers="SNTS", relevance=3, category="other")
    ctx = svc.gather_context(date(2026, 8, 20), min_relevance=6)
    assert all(n["relevance"] >= 6 for n in ctx["news"])
    assert len(ctx["news"]) == 1  # only the original relevance-8 item


# --- generate_for ---------------------------------------------------------


def test_generate_happy_path_persists_and_bills(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    client = FakeAnthropic([
        reply("# Session recap\nSNTS +4.5% led the tape.\n"
              "# Movers\n- SNTS +4.5% at 32,500 XOF\n"
              "# News that matters\n- SNTS: earnings\n"
              "# Watch tomorrow\n- (none)\n",
              input_tokens=2000, output_tokens=200),
    ])

    result = svc.generate_for(date(2026, 8, 20), client=client)
    assert result.brief is not None
    assert result.brief.day == "2026-08-20"
    assert result.brief.title == "Session recap"
    assert "SNTS" in result.brief.markdown
    assert result.brief.input_tokens == 2000
    assert result.brief.output_tokens == 200
    assert result.brief.usd_micros > 0

    # Spend counter got the same billing.
    _db_path_arg = _db_path
    with connect(_db_path_arg) as conn:
        spent = spend_repo.spent_micros(conn, "2026-08-20", table="brief_spend")
    assert spent == result.brief.usd_micros

    # Context JSON round-trips.
    ctx = json.loads(result.brief.context_json)
    assert ctx["day"] == "2026-08-20"


def test_generate_overwrites_same_day(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    client = FakeAnthropic([
        reply("# Session recap\nfirst", input_tokens=1000, output_tokens=100),
        reply("# Session recap\nsecond", input_tokens=1000, output_tokens=100),
    ])
    svc.generate_for(date(2026, 8, 20), client=client)
    second = svc.generate_for(date(2026, 8, 20), client=client)
    assert "second" in second.brief.markdown
    all_briefs = svc.list_recent_briefs(limit=10)
    assert len(all_briefs) == 1


def test_generate_dry_run_spends_nothing(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    result = svc.generate_for(date(2026, 8, 20), dry_run=True)
    assert result.dry_run is True
    assert result.brief is None
    assert result.usage is None or result.usage.usd_micros == 0
    assert svc.latest_brief() is None


def test_generate_no_api_key_is_a_warning_not_a_crash(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    reset_settings_cache()
    result = svc.generate_for(date(2026, 8, 20))
    assert result.llm_disabled is True
    assert result.brief is None


def test_generate_stops_when_budget_is_exhausted(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    with connect(_db_path) as conn:
        # Burn the whole $0.50 (50 * 10_000 = 500_000 micros) before we
        # start.
        spend_repo.add_usage(
            conn, input_tokens=0, output_tokens=0,
            usd_micros=500_000, table="brief_spend", day="2026-08-20",
        )

    client = FakeAnthropic([])  # would raise if called
    result = svc.generate_for(date(2026, 8, 20), client=client)
    assert result.budget_exhausted is True
    assert result.brief is None
    assert client.call_count == 0


def test_generate_empty_reply_bills_but_marks_failed(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    client = FakeAnthropic([reply("", input_tokens=1500, output_tokens=0)])

    result = svc.generate_for(date(2026, 8, 20), client=client)
    assert result.failed is True
    assert result.brief is None
    with connect(_db_path) as conn:
        spent = spend_repo.spent_micros(conn, "2026-08-20", table="brief_spend")
    # Empty reply still cost input tokens — must be billed.
    assert spent > 0


def test_generate_transport_error_is_reported_not_billed(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)

    class _Boom:
        class messages:
            @staticmethod
            def create(**_kw):
                raise RuntimeError("simulated 503")

    result = svc.generate_for(date(2026, 8, 20), client=_Boom())
    assert result.failed is True
    assert result.reason.startswith("transport_error")
    with connect(_db_path) as conn:
        spent = spend_repo.spent_micros(conn, "2026-08-20", table="brief_spend")
    assert spent == 0


def test_read_helpers_pull_from_the_repo(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    client = FakeAnthropic([
        reply("# hi\nbody", input_tokens=100, output_tokens=50),
    ])
    svc.generate_for(date(2026, 8, 20), client=client)
    assert svc.latest_brief().day == "2026-08-20"
    assert svc.get_brief("2026-08-20") is not None
    assert svc.get_brief("2026-08-19") is None
    assert len(svc.list_recent_briefs()) == 1
