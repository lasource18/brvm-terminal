"""Phase 6c: analyst-notes store + service.

Covers:

* `iso_week_monday` (pure)
* `store/analyst_notes` — upsert (overwrite same week), get, latest, list.
* `services/analyst_notes.gather_context` — shape + non-equity None.
* `services/analyst_notes.generate_for_ticker` — happy path, overwrite,
  dry-run, no-key, budget cap, empty-reply billing, transport error, and
  the non-equity short-circuit.
* `generate_for_all` — walks every active equity, honours the budget cap
  by skipping the tail, respects `limit=`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from brvm.config import reset_settings_cache
from brvm.db import connect
from brvm.models import (
    AnalystNote,
    IndexLevel,
    NewsItem,
    Quote,
    Security,
)
from brvm.sources._dedupe import news_hash
from brvm.store import analyst_notes as notes_repo
from brvm.store import news as news_repo
from brvm.store import quotes as quotes_repo
from brvm.store import securities as sec_repo
from brvm.store import spend as spend_repo

from ._fake_anthropic import FakeAnthropic, reply
from .conftest import apply_migrations


def _setup(monkeypatch, tmp_path: Path):
    """Fresh DB + three tickers + one snapshot + one tagged news row."""
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from brvm.services import analyst_notes as svc
    from brvm.services import history as history_mod
    from brvm.services import llm as llm_mod

    llm_mod.reset_client()
    history_mod.clear_cache()

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
            Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
            Security(ticker="SPHC", name="SAPH CI", kind="equity", country="CI"),
            Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
        ])
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="SNTS", source="sikafinance", last=32_500, change_pct=4.5,
                  volume=3000, turnover=97_500_000),
            Quote(ticker="ORAC", source="sikafinance", last=19_000, change_pct=-3.2,
                  volume=1000, turnover=19_000_000),
            Quote(ticker="SPHC", source="sikafinance", last=8_990, change_pct=1.1,
                  volume=500, turnover=4_495_000),
        ])
        quotes_repo.upsert_index_levels(conn, [
            IndexLevel(ticker="BRVMC", session_date=date(2026, 8, 20),
                       level=307.42, change_pct=0.53, source="sikafinance"),
        ])
        _seed_news(conn, title="SONATEL H1 results",
                   published_at="2026-08-20T09:00:00Z", tickers="SNTS",
                   relevance=8, category="earnings")
    # Stub the price-history fetcher so the service never touches the
    # network during a test.
    from brvm.sources import sikafinance
    monkeypatch.setattr(sikafinance, "fetch_historique",
                        lambda ticker, country, client=None: [])
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


# --- iso week -------------------------------------------------------------


def test_iso_week_monday_maps_any_day_to_its_monday():
    from brvm.services.analyst_notes import iso_week_monday
    # 2026-08-24 is a Monday, and 08-30 is the following Sunday of the
    # same ISO week.
    assert iso_week_monday(date(2026, 8, 24)) == date(2026, 8, 24)
    assert iso_week_monday(date(2026, 8, 30)) == date(2026, 8, 24)
    # A Tuesday of a different week rolls to its Monday too.
    assert iso_week_monday(date(2026, 8, 18)) == date(2026, 8, 17)


# --- store ----------------------------------------------------------------


def _mk_note(ticker: str = "SNTS", week: str = "2026-08-24",
             markdown: str = "# Snapshot\nBody.") -> AnalystNote:
    return AnalystNote(
        ticker=ticker, week_start=week, model="test-model", title="Snapshot",
        markdown=markdown, context_json="{}",
        input_tokens=1000, output_tokens=500, usd_micros=15_000,
        generated_utc="2026-08-24T20:00:00Z",
    )


def test_upsert_then_get(monkeypatch, tmp_path):
    db_path, _svc = _setup(monkeypatch, tmp_path)
    with connect(db_path) as conn:
        notes_repo.upsert(conn, _mk_note())
        got = notes_repo.get(conn, "SNTS", "2026-08-24")
    assert got is not None
    assert got.title == "Snapshot"


def test_upsert_overwrites_same_week(monkeypatch, tmp_path):
    db_path, _svc = _setup(monkeypatch, tmp_path)
    with connect(db_path) as conn:
        notes_repo.upsert(conn, _mk_note(markdown="first"))
        notes_repo.upsert(conn, _mk_note(markdown="second"))
        got = notes_repo.get(conn, "SNTS", "2026-08-24")
        n = notes_repo.count(conn)
    assert got.markdown == "second"
    assert n == 1


def test_latest_and_list_for_ticker(monkeypatch, tmp_path):
    db_path, _svc = _setup(monkeypatch, tmp_path)
    with connect(db_path) as conn:
        for w in ("2026-08-10", "2026-08-17", "2026-08-24"):
            notes_repo.upsert(conn, _mk_note(week=w))
        # A different ticker's row must not surface for SNTS reads.
        notes_repo.upsert(conn, _mk_note(ticker="ORAC", week="2026-08-24"))
        assert notes_repo.latest_for_ticker(conn, "SNTS").week_start == "2026-08-24"
        weeks = [n.week_start for n in notes_repo.list_for_ticker(conn, "SNTS", limit=2)]
        assert weeks == ["2026-08-24", "2026-08-17"]


# --- gather_context -------------------------------------------------------


def test_gather_context_shape(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    ctx = svc.gather_context("SNTS", week_start=date(2026, 8, 24))
    assert ctx is not None
    assert ctx["ticker"] == "SNTS"
    assert ctx["week_start"] == "2026-08-24"
    assert ctx["security"]["name"] == "SONATEL"
    assert ctx["quote"] is not None and ctx["quote"]["last"] == 32_500
    # The one tagged news item is within the 30-day window and appears.
    assert any(n["title"] == "SONATEL H1 results" for n in ctx["news"])


def test_gather_context_none_for_indices(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    assert svc.gather_context("BRVMC") is None


def test_gather_context_none_for_unknown_ticker(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    assert svc.gather_context("NOPE") is None


# --- generate_for_ticker --------------------------------------------------


def test_generate_happy_path_persists_and_bills(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    client = FakeAnthropic([
        reply("# Snapshot\nSonatel is Senegal's incumbent telecom.\n"
              "# Recent developments\n- H1 results in line.\n"
              "# Financial position\nRevenue up 8%.\n"
              "# Ratios read-across\nP/E in-line vs peers.\n"
              "# Risks & watch items\n- Regulatory\n",
              input_tokens=4000, output_tokens=800),
    ])
    result = svc.generate_for_ticker(
        "SNTS", week_start=date(2026, 8, 24), client=client,
    )
    assert result.note is not None
    assert result.note.week_start == "2026-08-24"
    assert result.note.title == "Snapshot"
    assert result.note.input_tokens == 4000
    assert result.note.output_tokens == 800
    assert result.note.usd_micros > 0

    # Spend counter got the same billing on today's row (utcnow-based —
    # billing follows real time, not the week the note covers).
    from brvm.clock import utcnow
    today = utcnow().date().isoformat()
    with connect(db_path) as conn:
        spent = spend_repo.spent_micros(conn, today, table="note_spend")
    assert spent == result.note.usd_micros

    # Context round-trips through JSON.
    parsed = json.loads(result.note.context_json)
    assert parsed["ticker"] == "SNTS"


def test_generate_overwrites_same_week(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    client = FakeAnthropic([
        reply("# Snapshot\nfirst", input_tokens=1000, output_tokens=100),
        reply("# Snapshot\nsecond", input_tokens=1000, output_tokens=100),
    ])
    svc.generate_for_ticker("SNTS", week_start=date(2026, 8, 24), client=client)
    result = svc.generate_for_ticker(
        "SNTS", week_start=date(2026, 8, 24), client=client,
    )
    assert "second" in result.note.markdown
    assert len(svc.list_notes("SNTS")) == 1


def test_generate_dry_run_spends_nothing(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    result = svc.generate_for_ticker(
        "SNTS", week_start=date(2026, 8, 24), dry_run=True,
    )
    assert result.dry_run is True
    assert result.note is None
    assert svc.latest_note("SNTS") is None


def test_generate_no_api_key_is_a_warning_not_a_crash(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    reset_settings_cache()
    result = svc.generate_for_ticker("SNTS", week_start=date(2026, 8, 24))
    assert result.llm_disabled is True
    assert result.note is None


def test_generate_stops_when_budget_is_exhausted(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    from brvm.clock import utcnow
    today = utcnow().date()
    with connect(db_path) as conn:
        # Burn the whole $3.00 (300 * 10_000 = 3_000_000 micros) before we
        # start.
        spend_repo.add_usage(
            conn, input_tokens=0, output_tokens=0,
            usd_micros=3_000_000, day=today, table="note_spend",
        )

    client = FakeAnthropic([])  # would raise if called
    result = svc.generate_for_ticker(
        "SNTS", week_start=date(2026, 8, 24), client=client,
    )
    assert result.budget_exhausted is True
    assert client.call_count == 0


def test_generate_empty_reply_bills_but_marks_failed(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    client = FakeAnthropic([reply("", input_tokens=1500, output_tokens=0)])

    result = svc.generate_for_ticker(
        "SNTS", week_start=date(2026, 8, 24), client=client,
    )
    assert result.failed is True
    assert result.note is None
    from brvm.clock import utcnow
    today = utcnow().date().isoformat()
    with connect(db_path) as conn:
        spent = spend_repo.spent_micros(conn, today, table="note_spend")
    # Empty reply still cost input tokens — must be billed.
    assert spent > 0


def test_generate_transport_error_is_reported_not_billed(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)

    class _Boom:
        class messages:
            @staticmethod
            def create(**_kw):
                raise RuntimeError("simulated 503")

    result = svc.generate_for_ticker(
        "SNTS", week_start=date(2026, 8, 24), client=_Boom(),
    )
    assert result.failed is True
    assert result.reason.startswith("transport_error")
    from brvm.clock import utcnow
    today = utcnow().date().isoformat()
    with connect(db_path) as conn:
        spent = spend_repo.spent_micros(conn, today, table="note_spend")
    assert spent == 0


def test_generate_for_index_short_circuits_as_failed(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    result = svc.generate_for_ticker(
        "BRVMC", week_start=date(2026, 8, 24), client=FakeAnthropic([]),
    )
    assert result.failed is True
    assert result.reason == "not_equity_or_unknown_ticker"


# --- generate_for_all -----------------------------------------------------


def _endless_client() -> FakeAnthropic:
    """A client that always replies to whatever ticker it's called for."""
    from tests._fake_anthropic import _Endless

    def handler(kwargs):
        return reply("# Snapshot\nnote", input_tokens=1000, output_tokens=200)

    c = FakeAnthropic()
    c._replies = _Endless(handler)  # type: ignore[assignment]
    return c


def test_generate_for_all_walks_every_equity(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    counts = svc.generate_for_all(
        week_start=date(2026, 8, 24),
        client=_endless_client(),
        delay_between_s=0,
    )
    assert counts.considered == 3          # SNTS + ORAC + SPHC (BRVMC excluded)
    assert counts.generated == 3
    assert sorted(counts.tickers_generated) == ["ORAC", "SNTS", "SPHC"]
    assert counts.failed == 0
    assert counts.total_usd_micros > 0


def test_generate_for_all_limit_caps_tickers(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    counts = svc.generate_for_all(
        week_start=date(2026, 8, 24),
        client=_endless_client(),
        delay_between_s=0,
        limit=1,
    )
    assert counts.considered == 1
    assert counts.generated == 1


def test_generate_for_all_stops_on_budget_exhaustion(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    # Burn most of the budget so the first ticker succeeds but the second
    # trips the cap.
    from brvm.clock import utcnow
    today = utcnow().date()
    with connect(db_path) as conn:
        spend_repo.add_usage(
            conn, input_tokens=0, output_tokens=0,
            usd_micros=2_997_000, day=today, table="note_spend",
        )
    counts = svc.generate_for_all(
        week_start=date(2026, 8, 24),
        client=_endless_client(),
        delay_between_s=0,
    )
    # SNTS lands (first ticker alphabetically), then the cap kicks in.
    assert counts.generated <= 1
    assert counts.skipped_budget >= 1


def test_generate_for_all_dry_run_reports_but_persists_nothing(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    counts = svc.generate_for_all(
        week_start=date(2026, 8, 24),
        dry_run=True,
        delay_between_s=0,
    )
    assert counts.considered == 3
    assert counts.dry_run_count == 3
    assert counts.generated == 0
    assert svc.latest_note("SNTS") is None
