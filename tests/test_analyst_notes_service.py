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
from datetime import date, timedelta
from pathlib import Path

from brvm.config import reset_settings_cache
from brvm.db import connect
from brvm.models import (
    AnalystNote,
    CorporateAction,
    IndexLevel,
    NewsItem,
    Quote,
    Security,
)
from brvm.services._view import PeersView
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
    # Stub the peer fetch (Phase 6d competitive analysis). Individual
    # tests can override this by re-patching `company_svc.get_peers_with_ratios`.
    from brvm.services import company as company_svc
    monkeypatch.setattr(
        company_svc, "get_peers_with_ratios",
        lambda ticker: PeersView(sector=None, source="none", peers=[]),
    )
    monkeypatch.setattr(
        svc.company_svc, "get_peers_with_ratios",
        lambda ticker: PeersView(sector=None, source="none", peers=[]),
    )
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


def test_gather_context_includes_peers_and_corporate_actions(monkeypatch, tmp_path):
    """Phase 6d: peer + corp-action data must land in the LLM snapshot."""
    db_path, svc = _setup(monkeypatch, tmp_path)
    from brvm.services._view import PeerRow

    def _peers(ticker):
        return PeersView(
            sector="Telecommunications",
            source="sikafinance",
            peers=[
                PeerRow(ticker="ORAC", name="ORANGE CI", country="CI",
                        last=19_000, change_ytd_pct=3.1, pe=8.9, roe=15.2,
                        net_margin=12.0),
                PeerRow(ticker="ETIT", name="ECOBANK ETI", country="TG",
                        last=17, change_ytd_pct=-4.2),
                # Self row should be filtered out of the context payload.
                PeerRow(ticker="SNTS", name="SONATEL", country="SN",
                        last=32_500, is_self=True),
            ],
        )

    monkeypatch.setattr(svc.company_svc, "get_peers_with_ratios", _peers)
    with connect(db_path) as conn:
        news_repo.upsert_corporate_actions(conn, [CorporateAction(
            ticker="SNTS", kind="dividend", ex_date=date(2026, 9, 15),
            pay_date=date(2026, 9, 30), amount=1_400.0, currency="XOF",
            yield_pct=4.3, source="sikafinance", source_url="https://x/div",
        )])

    ctx = svc.gather_context("SNTS", week_start=date(2026, 8, 24))
    assert ctx is not None
    peers = ctx["peers"]
    assert peers["sector"] == "Telecommunications"
    # Self row excluded; ratio-annotated peer preserved.
    tickers = [p["ticker"] for p in peers["peers"]]
    assert tickers == ["ORAC", "ETIT"]
    assert peers["peers"][0]["pe"] == 8.9
    # Corporate action lands too.
    actions = ctx["corporate_actions"]
    assert len(actions) == 1
    assert actions[0]["kind"] == "dividend"
    assert actions[0]["ex_date"] == "2026-09-15"


def test_generate_skips_when_context_unchanged(monkeypatch, tmp_path):
    """A previous note with the same fingerprint (news ids, financials
    periods, CA ids) short-circuits the LLM call for the new week.

    Both weeks used here fall within the seeded news item's 30-day lookback
    window so the news set is identical across weeks."""
    _db_path, svc = _setup(monkeypatch, tmp_path)
    client = FakeAnthropic([
        reply("# Snapshot\nfirst", input_tokens=1000, output_tokens=200),
    ])
    # Week 1 — a note is generated.
    r1 = svc.generate_for_ticker(
        "SNTS", week_start=date(2026, 8, 24), client=client,
    )
    assert r1.note is not None
    assert client.call_count == 1
    # Week 2 — nothing changed upstream, so we skip.
    r2 = svc.generate_for_ticker(
        "SNTS", week_start=date(2026, 8, 31), client=client,
    )
    assert r2.skipped_no_change is True
    assert r2.note is None
    assert r2.reason == "no_change_since_2026-08-24"
    assert client.call_count == 1  # no second call
    # Same-week rerun still regenerates (a mid-week refresh is expected).
    r3 = svc.generate_for_ticker(
        "SNTS", week_start=date(2026, 8, 24), client=FakeAnthropic([
            reply("# Snapshot\nrefresh", input_tokens=1000, output_tokens=200),
        ]),
    )
    assert r3.note is not None
    assert r3.skipped_no_change is False


def test_generate_regenerates_when_new_news_arrives(monkeypatch, tmp_path):
    """A fresh tagged news row for the ticker breaks the fingerprint tie
    and forces a new generation."""
    db_path, svc = _setup(monkeypatch, tmp_path)
    client = FakeAnthropic([
        reply("# Snapshot\nfirst", input_tokens=1000, output_tokens=200),
        reply("# Snapshot\nsecond", input_tokens=1000, output_tokens=200),
    ])
    svc.generate_for_ticker("SNTS", week_start=date(2026, 8, 24), client=client)

    # Add a new tagged news row before the second week's run.
    with connect(db_path) as conn:
        _seed_news(conn, title="SONATEL AGM notice",
                   published_at="2026-08-28T09:00:00Z", tickers="SNTS",
                   relevance=6, category="governance")

    r2 = svc.generate_for_ticker(
        "SNTS", week_start=date(2026, 8, 31), client=client,
    )
    assert r2.note is not None
    assert r2.skipped_no_change is False
    assert client.call_count == 2


def test_generate_force_bypasses_no_change_skip(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    client = FakeAnthropic([
        reply("# Snapshot\nfirst", input_tokens=1000, output_tokens=200),
        reply("# Snapshot\nforced", input_tokens=1000, output_tokens=200),
    ])
    svc.generate_for_ticker("SNTS", week_start=date(2026, 8, 24), client=client)
    r = svc.generate_for_ticker(
        "SNTS", week_start=date(2026, 8, 31), client=client, force=True,
    )
    assert r.note is not None
    assert r.skipped_no_change is False


def test_generate_for_all_counts_no_change_skips(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    first = svc.generate_for_all(
        week_start=date(2026, 8, 24),
        client=_endless_client(),
        delay_between_s=0,
    )
    assert first.generated == 3
    # No upstream changes → every ticker gets skipped on the second pass.
    second = svc.generate_for_all(
        week_start=date(2026, 8, 31),
        client=_endless_client(),
        delay_between_s=0,
    )
    assert second.generated == 0
    assert second.skipped_no_change == 3
    assert second.total_usd_micros == 0


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


# --- Phase 8e: sector-median peer table -----------------------------------


class TestPeerMedians:
    """Pure helper — direct dict inputs, no DB."""

    def test_returns_empty_when_no_rows(self):
        from brvm.services.analyst_notes import _peer_medians
        assert _peer_medians([]) == {}

    def test_omits_fields_with_one_sample(self):
        from brvm.services.analyst_notes import _peer_medians
        rows = [
            {"pe": 10.0, "roe": None, "net_margin": None,
             "change_ytd_pct": 3.0, "market_cap": None},
            {"pe": None, "roe": 15.0, "net_margin": 12.0,
             "change_ytd_pct": None, "market_cap": None},
        ]
        med = _peer_medians(rows)
        # Every field has ≤1 non-None sample → all dropped.
        assert med == {}

    def test_median_of_odd_and_even_samples(self):
        from brvm.services.analyst_notes import _peer_medians
        rows = [
            {"pe": 8.0, "roe": None, "net_margin": None,
             "change_ytd_pct": None, "market_cap": None},
            {"pe": 12.0, "roe": None, "net_margin": None,
             "change_ytd_pct": None, "market_cap": None},
            {"pe": 10.0, "roe": None, "net_margin": None,
             "change_ytd_pct": None, "market_cap": None},
        ]
        med = _peer_medians(rows)
        assert med == {"pe": 10.0}
        # Add a fourth peer → median of 8/10/12/14 = 11 (mean of the two middles).
        rows.append({"pe": 14.0, "roe": None, "net_margin": None,
                     "change_ytd_pct": None, "market_cap": None})
        med2 = _peer_medians(rows)
        assert med2 == {"pe": 11.0}

    def test_covers_all_expected_fields(self):
        from brvm.services.analyst_notes import _peer_medians
        rows = [
            {"pe": 8.0, "roe": 10.0, "net_margin": 5.0,
             "change_ytd_pct": 2.0, "market_cap": 1_000.0},
            {"pe": 12.0, "roe": 20.0, "net_margin": 15.0,
             "change_ytd_pct": 6.0, "market_cap": 3_000.0},
        ]
        med = _peer_medians(rows)
        assert med == {
            "pe": 10.0, "roe": 15.0, "net_margin": 10.0,
            "change_ytd_pct": 4.0, "market_cap": 2_000.0,
        }


def test_gather_context_peers_block_includes_medians_and_self(monkeypatch, tmp_path):
    """Peer block should carry a `medians` dict (Python-computed) and a
    `self` dict (the subject's own ratios) so Sonnet can do a concrete
    self-vs-median compare without inventing numbers."""
    _db_path, svc = _setup(monkeypatch, tmp_path)
    from brvm.services._view import PeerRow

    def _peers(ticker):
        return PeersView(
            sector="Telecommunications",
            source="sikafinance",
            peers=[
                PeerRow(ticker="ORAC", name="ORANGE CI", country="CI",
                        last=19_000, change_ytd_pct=3.1, pe=8.9, roe=15.2,
                        net_margin=12.0, market_cap=200_000_000),
                PeerRow(ticker="ETIT", name="ECOBANK ETI", country="TG",
                        last=17, change_ytd_pct=-4.2, pe=6.5, roe=11.0,
                        net_margin=8.0, market_cap=400_000_000),
                PeerRow(ticker="ONTBF", name="ONATEL BF", country="BF",
                        last=3_500, change_ytd_pct=1.0, pe=10.0, roe=13.0,
                        net_margin=10.0, market_cap=300_000_000),
                # Subject row (is_self) provides the self-vs-median block.
                PeerRow(ticker="SNTS", name="SONATEL", country="SN",
                        last=32_500, change_ytd_pct=5.0, pe=15.0, roe=20.0,
                        net_margin=18.0, market_cap=600_000_000, is_self=True),
            ],
        )

    monkeypatch.setattr(svc.company_svc, "get_peers_with_ratios", _peers)
    ctx = svc.gather_context("SNTS", week_start=date(2026, 8, 24))
    assert ctx is not None
    peers = ctx["peers"]
    med = peers["medians"]
    # Medians computed over the three peer rows (self is excluded).
    assert med["pe"] == 8.9        # median of 8.9 / 6.5 / 10.0
    assert med["roe"] == 13.0      # median of 15.2 / 11.0 / 13.0
    assert med["net_margin"] == 10.0
    assert med["change_ytd_pct"] == 1.0
    assert med["market_cap"] == 300_000_000
    # Subject's own numbers live in `self` for a direct compare.
    assert peers["self"]["pe"] == 15.0
    assert peers["self"]["roe"] == 20.0
    # Peer list itself still excludes the self row.
    assert [p["ticker"] for p in peers["peers"]] == ["ORAC", "ETIT", "ONTBF"]


def test_gather_context_peers_medians_empty_when_all_null(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    from brvm.services._view import PeerRow

    def _peers(ticker):
        # Every peer has null ratios → no field clears the ≥2-sample floor.
        return PeersView(
            sector="Telecom",
            source="sikafinance",
            peers=[
                PeerRow(ticker="ORAC", name="ORANGE CI", country="CI", last=19_000),
                PeerRow(ticker="ETIT", name="ECOBANK ETI", country="TG", last=17),
            ],
        )

    monkeypatch.setattr(svc.company_svc, "get_peers_with_ratios", _peers)
    ctx = svc.gather_context("SNTS", week_start=date(2026, 8, 24))
    assert ctx is not None
    assert ctx["peers"]["medians"] == {}


def test_gather_context_peers_medians_absent_when_fetch_fails(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)

    def _raises(ticker):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(svc.company_svc, "get_peers_with_ratios", _raises)
    ctx = svc.gather_context("SNTS", week_start=date(2026, 8, 24))
    assert ctx is not None
    peers = ctx["peers"]
    assert peers["peers"] == []
    assert peers["medians"] == {}
    # `self` isn't emitted when the fetch fails — there's no subject row
    # to draw from — so downstream renderers can rely on its presence
    # implying a live peer set.
    assert "self" not in peers


# --- Phase 7b: price-stats enrichment (max drawdown + beta vs BRVMC) -----


class TestMaxDrawdown:
    """Pure helper — no DB / fixtures."""

    def test_none_when_series_too_short(self):
        from brvm.services.analyst_notes import _max_drawdown_pct
        assert _max_drawdown_pct([]) is None
        assert _max_drawdown_pct([100.0]) is None

    def test_monotonic_series_is_zero(self):
        from brvm.services.analyst_notes import _max_drawdown_pct
        # Newest-first ascending (i.e. price only went up chronologically).
        assert _max_drawdown_pct([120.0, 110.0, 100.0]) == 0.0

    def test_measures_peak_to_trough(self):
        # Chronological: 100 → 90 → 110 → 80. Peak 110, trough 80 →
        # 27.27% drawdown. Series comes in newest-first.
        from brvm.services.analyst_notes import _max_drawdown_pct
        dd = _max_drawdown_pct([80.0, 110.0, 90.0, 100.0])
        assert dd is not None
        assert abs(dd - 27.2727272727) < 1e-6

    def test_peak_only_ratchets_from_prior_prices(self):
        # A single early spike should still register — the walk is
        # chronological and the peak ratchet cannot look backwards.
        from brvm.services.analyst_notes import _max_drawdown_pct
        # Chronological: 100 → 200 → 150 → 180. Peak 200, trough 150 = 25%.
        dd = _max_drawdown_pct([180.0, 150.0, 200.0, 100.0])
        assert dd is not None
        assert abs(dd - 25.0) < 1e-6


class TestBetaVsMarket:
    """Pure helper — feed it aligned bar lists directly."""

    def _bars(self, closes: list[float], start_date: date):
        # Newest-first bars: closes[0] is the latest session.
        from brvm.models import DailyBar
        n = len(closes)
        return [
            DailyBar(
                ticker="X", session_date=start_date - timedelta(days=n - 1 - i),
                close=closes[i], source="test",
            )
            for i in range(n)
        ]

    def test_none_when_market_missing(self):
        from brvm.services.analyst_notes import _beta_vs_market
        stock = self._bars([100.0, 99.0, 98.0], date(2026, 8, 27))
        assert _beta_vs_market(stock, None) is None
        assert _beta_vs_market(stock, []) is None

    def test_none_when_fewer_than_20_aligned_returns(self):
        from brvm.services.analyst_notes import _beta_vs_market
        stock = self._bars([100 + i for i in range(15)], date(2026, 8, 27))
        market = self._bars([50 + i for i in range(15)], date(2026, 8, 27))
        # 15 bars → 14 returns → below the 20-return floor.
        assert _beta_vs_market(stock, market) is None

    def test_beta_equals_scaling_when_stock_is_scaled_market(self):
        # Stock = 2x market (exact linear scaling) → beta = 2.
        # 25 sessions gives 24 aligned returns, over the floor.
        import math

        from brvm.services.analyst_notes import _beta_vs_market
        n = 25
        # Build a market with non-flat returns so var(market) > 0.
        # Use a simple random-ish sinusoid so the covariance is exact.
        market_closes = [100 * (1 + 0.02 * math.sin(i / 3)) for i in range(n)]
        stock_closes = [50 * m / 100 for m in market_closes]  # stock = 0.5 * market → beta = 1
        # Reverse for newest-first shape.
        market_closes = list(reversed(market_closes))
        stock_closes = list(reversed(stock_closes))
        market_bars = self._bars(market_closes, date(2026, 8, 27))
        stock_bars = self._bars(stock_closes, date(2026, 8, 27))
        beta = _beta_vs_market(stock_bars, market_bars)
        assert beta is not None
        # Perfect linear scaling → beta = 1 regardless of the multiplier
        # (because log returns are scale-invariant).
        assert abs(beta - 1.0) < 1e-9

    def test_beta_positive_when_series_move_together(self):
        # Different-magnitude but correlated series → positive beta.
        import math

        from brvm.services.analyst_notes import _beta_vs_market
        n = 30
        market_closes = list(reversed(
            [100 * (1 + 0.01 * math.sin(i / 2)) for i in range(n)]
        ))
        # Stock moves in the same direction with 2x sensitivity:
        # stock_ret = 2 * market_ret => stock_close = market_close ** 2 (scaled).
        market_chrono = list(reversed(market_closes))
        stock_chrono: list[float] = [1000.0]
        for i in range(1, n):
            m_ret = math.log(market_chrono[i] / market_chrono[i - 1])
            stock_chrono.append(stock_chrono[i - 1] * math.exp(2 * m_ret))
        stock_closes = list(reversed(stock_chrono))
        beta = _beta_vs_market(
            self._bars(stock_closes, date(2026, 8, 27)),
            self._bars(market_closes, date(2026, 8, 27)),
        )
        assert beta is not None
        assert abs(beta - 2.0) < 1e-6

    def test_beta_alignment_ignores_market_only_sessions(self):
        # Market has a session the stock doesn't — that day must be
        # dropped from the alignment rather than mis-pairing consecutive
        # stock closes with non-consecutive market closes.
        import math

        from brvm.models import DailyBar
        from brvm.services.analyst_notes import _beta_vs_market
        n = 30
        market_chrono = [100 * (1 + 0.01 * math.sin(i / 2)) for i in range(n)]
        stock_chrono = [c * 0.5 for c in market_chrono]

        base = date(2026, 8, 27)
        # Build bars newest-first with matching dates on both sides
        # except for one extra market session on day base-100 (not in
        # stock's window).
        market_bars = [
            DailyBar(ticker="BRVMC", session_date=base - timedelta(days=n - 1 - i),
                     close=market_chrono[i], source="test")
            for i in range(n)
        ]
        market_bars.append(DailyBar(ticker="BRVMC",
                                    session_date=base - timedelta(days=200),
                                    close=99.0, source="test"))
        stock_bars = [
            DailyBar(ticker="X", session_date=base - timedelta(days=n - 1 - i),
                     close=stock_chrono[i], source="test")
            for i in range(n)
        ]
        # Newest-first.
        stock_bars = list(reversed(stock_bars))
        market_bars = list(reversed(market_bars))
        beta = _beta_vs_market(stock_bars, market_bars)
        assert beta is not None
        assert abs(beta - 1.0) < 1e-9


class TestPriceStatsPayload:
    def test_max_drawdown_populated_when_series_present(self):
        from brvm.models import DailyBar
        from brvm.services.analyst_notes import _price_stats
        # Chronological: 100 → 200 → 150 (peak-to-trough = 25%).
        bars = [
            DailyBar(ticker="X", session_date=date(2026, 8, 27),
                     close=150.0, source="test"),
            DailyBar(ticker="X", session_date=date(2026, 8, 26),
                     close=200.0, source="test"),
            DailyBar(ticker="X", session_date=date(2026, 8, 25),
                     close=100.0, source="test"),
        ]
        stats = _price_stats(bars)
        assert stats is not None
        assert stats["max_drawdown_pct"] == 25.0

    def test_beta_populated_when_market_bars_supplied(self, monkeypatch, tmp_path):
        # Round-trip through gather_context — verifies the wiring, not
        # just the pure helpers.
        db_path, svc = _setup(monkeypatch, tmp_path)
        # Seed 30 aligned close pairs so beta clears the 20-return floor.
        import math

        from brvm.models import DailyBar
        n = 30
        base = date(2026, 8, 20)
        market_chrono = [300 + 5 * math.sin(i / 2) for i in range(n)]
        stock_chrono = [30000 + 500 * math.sin(i / 2) for i in range(n)]

        from brvm.models import IndexLevel
        with connect(db_path) as conn:
            quotes_repo.upsert_daily_bars(conn, [
                DailyBar(
                    ticker="SNTS",
                    session_date=base - timedelta(days=n - 1 - i),
                    close=stock_chrono[i], source="test",
                )
                for i in range(n)
            ])
            # BRVMC lives in index_levels; history.get_history reads it
            # from there when kind='index'.
            quotes_repo.upsert_index_levels(conn, [
                IndexLevel(
                    ticker="BRVMC",
                    session_date=base - timedelta(days=n - 1 - i),
                    level=market_chrono[i],
                    source="test",
                )
                for i in range(n)
            ])
        # history.get_history caches — reset so we pick up the fresh rows.
        from brvm.services import history as history_mod
        history_mod.clear_cache()
        ctx = svc.gather_context("SNTS", week_start=date(2026, 8, 24))
        assert ctx is not None
        ps = ctx["price_stats"]
        assert ps is not None
        # The stock closes and market levels are both scaled sinusoids —
        # `log(stock_i / stock_{i-1})` and `log(market_i / market_{i-1})`
        # are highly correlated but not identical, so beta is well-defined
        # and non-zero.
        assert "beta_vs_market" in ps
        assert ps["beta_vs_market"] > 0
        assert "max_drawdown_pct" in ps
