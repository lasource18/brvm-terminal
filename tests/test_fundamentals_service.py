"""Tests for services/fundamentals.py: the extraction worker + read helpers."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from brvm.config import reset_settings_cache
from brvm.db import connect
from brvm.models import Filing, Security
from brvm.store import filings as filings_repo
from brvm.store import financials as fin_repo
from brvm.store import securities as sec_repo
from brvm.store import spend as spend_repo

from ._fake_anthropic import FakeAnthropic, FakeResponse, reply
from .conftest import apply_migrations


def _happy() -> dict[str, Any]:
    return {
        "period_year": 2024,
        "period_kind": "annual",
        "currency": "XOF",
        "revenue": 1_500_000_000,
        "operating_income": 400_000_000,
        "net_income": 300_000_000,
        "total_assets": 5_000_000_000,
        "total_equity": 2_000_000_000,
        "eps": 1200.0,
        "dividend_per_share": 500.0,
        "segments": [
            {"name": "Sénégal", "segment_kind": "geo", "share_pct": 60.0},
        ],
        "ownership": [
            {"holder": "SONATEL SA", "share_pct": 42.3},
        ],
    }


def _json_reply(data: dict[str, Any], **usage: int) -> FakeResponse:
    return reply(json.dumps(data, ensure_ascii=False), **usage)


def _mk_filing(year: int = 2024) -> Filing:
    return Filing(
        ticker="SNTS",
        issuer_name="SONATEL",
        doc_type="rapport_annuel",
        period_kind="annual",
        period_year=year,
        source="brvm_org",
        source_url=f"https://brvm.org/{year}.pdf",
        url_hash=f"h-{year}",
        published_date=date(year + 1, 3, 15),
        file_path=f"data/filings/SNTS/{year}.pdf",
        size_bytes=1024,
        sha256="deadbeef",
        page_count=42,
    )


def _setup(monkeypatch, tmp_path: Path, *, n_filings: int = 1):
    """Fresh DB + one security + `n_filings` pending annual reports.

    Returns the reloaded (config-aware) worker module."""
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from brvm.services import extraction as ext_mod
    from brvm.services import fundamentals as svc

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN")])
        filings_repo.upsert_filings(conn, [_mk_filing(2024 - i) for i in range(n_filings)])

    # Force the pypdf path to return known text so tests are hermetic.
    def _fake_extract(path: Path, *, max_chars: int = 120_000):
        return ext_mod.PdfExtract(
            text="Chiffre d'affaires 1 500 000 000 FCFA " * 20,
            page_count=42,
            is_scanned=False,
        )

    monkeypatch.setattr(svc.extraction, "extract_pdf_text", _fake_extract)
    # And make sure the file existence check passes without a real PDF.
    monkeypatch.setattr(Path, "exists", lambda self: True)
    return db_path, svc


def test_extracts_a_pending_filing_and_records_spend(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    client = FakeAnthropic([_json_reply(_happy())])

    counts = svc.extract_pending(client=client, project_root=tmp_path)

    assert counts.pending_before == 1
    assert counts.considered == 1
    assert counts.extracted == 1
    assert counts.pending_after == 0
    assert counts.spend_micros_after == 3000

    with connect(db_path) as conn:
        fins = fin_repo.list_financials(conn, "SNTS")
        assert len(fins) == 1
        assert fins[0]["revenue"] == 1_500_000_000
        # Filing has been stamped so a second pass is a no-op.
        row = conn.execute("SELECT extracted_utc FROM filings").fetchone()
        assert row["extracted_utc"] is not None
        # Spend recorded against the *filings_spend* table, not llm_spend.
        assert spend_repo.get_day(conn, table="filings_spend")["calls"] == 1
        assert spend_repo.get_day(conn, table="llm_spend") is None


def test_second_pass_is_a_no_op(monkeypatch, tmp_path):
    _db, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    svc.extract_pending(client=FakeAnthropic([_json_reply(_happy())]), project_root=tmp_path)

    # No replies scripted — a call would raise.
    counts = svc.extract_pending(client=FakeAnthropic([]), project_root=tmp_path)
    assert counts.pending_before == 0
    assert counts.extracted == 0


def test_scanned_pdf_is_flagged_and_skipped(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)

    def _scanned(path: Path, *, max_chars: int = 120_000):
        return svc.extraction.PdfExtract(text="", page_count=200, is_scanned=True)

    monkeypatch.setattr(svc.extraction, "extract_pdf_text", _scanned)

    counts = svc.extract_pending(client=FakeAnthropic([]), project_root=tmp_path)
    assert counts.scanned == 1
    assert counts.extracted == 0

    with connect(db_path) as conn:
        row = conn.execute("SELECT is_scanned, extracted_utc FROM filings").fetchone()
        assert row["is_scanned"] == 1
        # Stamped too — otherwise the same PDF gets probed on every run.
        assert row["extracted_utc"] is not None


def test_daily_cap_stops_the_pass(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=3)
    with connect(db_path) as conn:
        # Burn the full $2 cap (200 cents = 2_000_000 micros).
        spend_repo.add_usage(
            conn, input_tokens=0, output_tokens=0, usd_micros=2_000_000, table="filings_spend"
        )

    counts = svc.extract_pending(client=FakeAnthropic([]), project_root=tmp_path)
    assert counts.extracted == 0
    assert counts.skipped_budget == 3
    # No filings were stamped — they should retry once budget resets.
    with connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM filings WHERE extracted_utc IS NULL").fetchone()[0]
            == 3
        )


def test_missing_file_on_disk_is_skipped(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    monkeypatch.setattr(Path, "exists", lambda self: False)

    counts = svc.extract_pending(client=FakeAnthropic([]), project_root=tmp_path)
    assert counts.skipped_missing_file == 1
    assert counts.extracted == 0
    with connect(db_path) as conn:
        # Not stamped — restoring the file from a re-poll should let it retry.
        row = conn.execute("SELECT extracted_utc FROM filings").fetchone()
        assert row["extracted_utc"] is None


def test_failed_call_stamps_filing_and_still_bills(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    client = FakeAnthropic([reply("garbage"), reply("more garbage")])

    counts = svc.extract_pending(client=client, project_root=tmp_path)

    assert counts.failed == 1
    assert counts.extracted == 0
    assert counts.spend_micros_after == 6000  # both retry attempts billed
    with connect(db_path) as conn:
        # Stamped so it's not retried again — the retry-on-parse already
        # burned two calls; a third would be waste.
        row = conn.execute("SELECT extracted_utc FROM filings").fetchone()
        assert row["extracted_utc"] is not None


def test_empty_extract_stamps_but_counts_as_empty(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    empty = {"period_kind": "annual", "currency": "XOF", "segments": [], "ownership": []}
    client = FakeAnthropic([_json_reply(empty)])

    counts = svc.extract_pending(client=client, project_root=tmp_path)

    assert counts.extracted == 0
    assert counts.empty_payloads == 1
    with connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM financials").fetchone()[0] == 0
        row = conn.execute("SELECT extracted_utc FROM filings").fetchone()
        assert row["extracted_utc"] is not None


def test_no_api_key_is_a_warning_not_a_crash(monkeypatch, tmp_path):
    _db, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    reset_settings_cache()

    counts = svc.extract_pending(project_root=tmp_path)

    assert counts.llm_disabled == 1
    assert counts.extracted == 0
    assert counts.pending_after == 1


def test_dry_run_spends_nothing_but_reports_would_have_extracted(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=2)

    counts = svc.extract_pending(dry_run=True, project_root=tmp_path, client=FakeAnthropic([]))

    assert counts.dry_run == 1
    assert counts.extracted == 2  # would-have-been
    assert counts.spend_micros_after == 0
    # Read-only: nothing stamped, so a real pass afterwards will process the
    # same rows.
    with connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM filings WHERE extracted_utc IS NULL").fetchone()[0]
            == 2
        )


def test_dry_run_does_not_stamp_scanned_pdfs(monkeypatch, tmp_path):
    """A first-pass discovery must not silently mutate the corpus."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)

    def _scanned(path: Path, *, max_chars: int = 120_000):
        return svc.extraction.PdfExtract(text="", page_count=200, is_scanned=True)

    monkeypatch.setattr(svc.extraction, "extract_pdf_text", _scanned)

    counts = svc.extract_pending(dry_run=True, project_root=tmp_path, client=FakeAnthropic([]))
    assert counts.scanned == 1
    with connect(db_path) as conn:
        row = conn.execute("SELECT is_scanned, extracted_utc FROM filings").fetchone()
        assert row["is_scanned"] is None
        assert row["extracted_utc"] is None


# --- read helpers ---------------------------------------------------------


def test_get_financials_series_returns_periods_descending(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    with connect(db_path) as conn:
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        for y in (2022, 2023, 2024):
            fin_repo.replace_period(
                conn,
                filing_id=filing_id,
                financials=fin_repo.FinancialsRow(
                    ticker="SNTS", period_year=y, revenue=1000 * y
                ),
            )

    series = svc.get_financials_series("SNTS")
    assert series.has_data is True
    assert series.periods == [2024, 2023, 2022]
    assert series.metrics["revenue"] == [2_024_000, 2_023_000, 2_022_000]


def test_get_financials_series_carries_per_period_currency(monkeypatch, tmp_path):
    """F-24: a period reported in a comparative currency must not be
    silently labelled with the newest row's currency. Prior behaviour
    pinned the whole table to `rows[0].currency` — an EUR row would
    then render under an ``amounts in XOF`` caption, misleading by
    ~655x."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    with connect(db_path) as conn:
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        fin_repo.replace_period(
            conn, filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2022, currency="EUR", revenue=2_200_000,
            ),
        )
        fin_repo.replace_period(
            conn, filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2023, currency="XOF", revenue=1_500_000_000,
            ),
        )
        fin_repo.replace_period(
            conn, filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2024, currency="XOF", revenue=1_600_000_000,
            ),
        )

    series = svc.get_financials_series("SNTS")
    # Currencies align with periods (newest → oldest).
    assert series.periods == [2024, 2023, 2022]
    assert series.currencies == ["XOF", "XOF", "EUR"]
    assert series.has_mixed_currencies is True


def test_get_financials_series_uniform_currency_is_not_mixed(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    with connect(db_path) as conn:
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        for y in (2022, 2023, 2024):
            fin_repo.replace_period(
                conn, filing_id=filing_id,
                financials=fin_repo.FinancialsRow(
                    ticker="SNTS", period_year=y, currency="XOF", revenue=1000 * y,
                ),
            )
    series = svc.get_financials_series("SNTS")
    assert series.currencies == ["XOF", "XOF", "XOF"]
    assert series.has_mixed_currencies is False
    assert series.currency == "XOF"


def test_get_segments_and_ownership_fall_back_to_latest_period_with_data(monkeypatch, tmp_path):
    """F-07: when the newest annual period has no segment/ownership rows
    (e.g. a statements-only ``etats_financiers`` was extracted for 2024
    but the ``rapport_annuel`` hasn't landed yet), the Segments and
    Ownership tabs must fall back to the previous year's still-persisted
    data instead of rendering an empty view."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    with connect(db_path) as conn:
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        # 2023: fully-populated segments + ownership.
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(ticker="SNTS", period_year=2023),
            segments=[fin_repo.SegmentRow(name="Mobile", segment_kind="business", share_pct=70)],
            ownership=[fin_repo.OwnershipRow(holder="SONATEL SA", share_pct=42.3)],
        )
        # 2024: bare P&L only — no segments, no ownership.
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2024, revenue=1_600_000_000,
            ),
        )

    seg = svc.get_segments("SNTS")
    assert seg.period_year == 2023
    assert [s["name"] for s in seg.business] == ["Mobile"]

    own = svc.get_ownership("SNTS")
    assert own.period_year == 2023
    assert [h["holder"] for h in own.holders] == ["SONATEL SA"]


def test_get_segments_and_ownership_return_the_latest_period(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    with connect(db_path) as conn:
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(ticker="SNTS", period_year=2023),
            segments=[fin_repo.SegmentRow(name="Old", segment_kind="business", share_pct=100)],
        )
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(ticker="SNTS", period_year=2024),
            segments=[
                fin_repo.SegmentRow(name="Mobile", segment_kind="business", share_pct=70),
                fin_repo.SegmentRow(name="Sénégal", segment_kind="geo", share_pct=60),
            ],
            ownership=[fin_repo.OwnershipRow(holder="SONATEL SA", share_pct=42.3)],
        )

    seg = svc.get_segments("SNTS")
    assert seg.has_data is True
    assert seg.period_year == 2024
    assert [s["name"] for s in seg.business] == ["Mobile"]
    assert [s["name"] for s in seg.geo] == ["Sénégal"]

    own = svc.get_ownership("SNTS")
    assert own.period_year == 2024
    assert [h["holder"] for h in own.holders] == ["SONATEL SA"]


def test_read_helpers_return_empty_views_when_nothing_extracted(monkeypatch, tmp_path):
    _db, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    assert svc.get_financials_series("SNTS").has_data is False
    assert svc.get_segments("SNTS").has_data is False
    assert svc.get_ownership("SNTS").has_data is False
    assert svc.get_latest_interim("SNTS") is None


# --- interim card (Phase 4c) ----------------------------------------------


def test_get_latest_interim_returns_most_recent_period_ahead_of_annual(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    with connect(db_path) as conn:
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        # Annual 2024 stops here; the interim card should surface H1 2025.
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(ticker="SNTS", period_year=2024, revenue=100),
        )
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2025, period_kind="Q1", revenue=25
            ),
        )
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2025, period_kind="H1", revenue=55
            ),
        )

    interim = svc.get_latest_interim("SNTS")
    assert interim is not None
    assert (interim.period_year, interim.period_kind) == (2025, "H1")
    assert interim.metrics["revenue"] == 55


def test_get_latest_interim_hides_stale_interim_when_annual_is_newer(monkeypatch, tmp_path):
    """Mixing an old H1 with a fresh full-year would mislead — hide it."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    with connect(db_path) as conn:
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2023, period_kind="H1", revenue=40
            ),
        )
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2024, revenue=100
            ),
        )

    assert svc.get_latest_interim("SNTS") is None


# --- reset_shadowed_extractions (recovery for pre-PR-#13 wipes) ------------


def _mk_filing_of(
    doc_type: str, *, ticker: str = "SNTS", period_year: int = 2025,
    period_kind: str = "annual",
) -> Filing:
    return Filing(
        ticker=ticker,
        issuer_name=ticker,
        doc_type=doc_type,  # type: ignore[arg-type]
        period_kind=period_kind,  # type: ignore[arg-type]
        period_year=period_year,
        source="brvm_org",
        source_url=f"https://brvm.org/{ticker}/{doc_type}-{period_year}.pdf",
        url_hash=f"hash-{ticker}-{doc_type}-{period_year}",
        published_date=date(period_year + 1, 3, 15),
        file_path=f"data/filings/{ticker}/{doc_type}-{period_year}.pdf",
        size_bytes=1024,
        sha256="deadbeef",
        page_count=42,
    )


def test_reset_shadowed_extractions_clears_richer_filings(monkeypatch, tmp_path):
    """The ORAC repro: rapport_annuel (rank 1) extracted first with
    shareholders, then etats_financiers (rank 2) extracted second and
    wiped ownership. Recovery must clear extracted_utc on the
    rapport_annuel so the next extract re-populates it."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=0)
    with connect(db_path) as conn:
        filings_repo.upsert_filings(conn, [
            _mk_filing_of("rapport_annuel"),
            _mk_filing_of("etats_financiers"),
        ])
        both = {r["doc_type"]: r["id"] for r in conn.execute(
            "SELECT id, doc_type FROM filings"
        ).fetchall()}
        for fid in both.values():
            filings_repo.mark_extracted(conn, fid)
        # Financials row points at the LESSER filing — the shadowed state.
        fin_repo.replace_period(
            conn,
            filing_id=both["etats_financiers"],
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2025, revenue=1_500_000_000
            ),
        )

    counts = svc.reset_shadowed_extractions()
    assert counts == {"periods_shadowed": 1, "filings_reset": 1, "dry_run": 0}

    with connect(db_path) as conn:
        rows = {r["doc_type"]: r["extracted_utc"] for r in conn.execute(
            "SELECT doc_type, extracted_utc FROM filings"
        ).fetchall()}
    # Rapport_annuel is now unstamped → next extract will re-process it.
    assert rows["rapport_annuel"] is None
    # Etats_financiers stays stamped — its P&L data is still in `financials`;
    # replace_period's preserve-on-empty logic will merge the rapport_annuel's
    # ownership + segments into the same row on the next pass.
    assert rows["etats_financiers"] is not None


def test_reset_shadowed_extractions_leaves_non_shadowed_alone(monkeypatch, tmp_path):
    """If the persisted filing_id ALREADY points at the richest filing
    for its period, there's nothing to unshadow."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=0)
    with connect(db_path) as conn:
        filings_repo.upsert_filings(conn, [
            _mk_filing_of("rapport_annuel"),
            _mk_filing_of("etats_financiers"),
        ])
        both = {r["doc_type"]: r["id"] for r in conn.execute(
            "SELECT id, doc_type FROM filings"
        ).fetchall()}
        for fid in both.values():
            filings_repo.mark_extracted(conn, fid)
        # Financials points at the RICHER filing (rapport_annuel).
        fin_repo.replace_period(
            conn,
            filing_id=both["rapport_annuel"],
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2025, revenue=1_500_000_000
            ),
        )

    counts = svc.reset_shadowed_extractions()
    assert counts["filings_reset"] == 0
    with connect(db_path) as conn:
        stamped = list(conn.execute(
            "SELECT 1 FROM filings WHERE extracted_utc IS NOT NULL"
        ).fetchall())
    assert len(stamped) == 2


def test_get_financials_source_filings_lists_one_per_extracted_period(monkeypatch, tmp_path):
    """The References subsection on the Financials tab renders exactly
    one row per `(period_year, period_kind)` currently persisted in
    `financials`, joined onto the filings table for the audit link."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=0)
    with connect(db_path) as conn:
        filings_repo.upsert_filings(conn, [
            _mk_filing(year=2024),
            _mk_filing(year=2023),
            _mk_filing(year=2022),
        ])
        by_year = {int(r["period_year"]): int(r["id"]) for r in conn.execute(
            "SELECT id, period_year FROM filings"
        ).fetchall()}
        for year, fid in by_year.items():
            fin_repo.replace_period(
                conn, filing_id=fid,
                financials=fin_repo.FinancialsRow(
                    ticker="SNTS", period_year=year, revenue=1_000_000,
                ),
            )

    refs = svc.get_financials_source_filings("SNTS")
    assert [r.period_year for r in refs] == [2024, 2023, 2022]
    for r in refs:
        assert r.source == "brvm_org"
        assert r.source_url.startswith("https://brvm.org/")
        assert r.doc_type == "rapport_annuel"


def test_get_financials_source_filings_prefers_annual_within_a_year(monkeypatch, tmp_path):
    """When both an annual and an interim row exist for the same year, the
    annual should come first in the reference list (users usually want the
    full-year audit trail before the interim)."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=0)
    with connect(db_path) as conn:
        filings_repo.upsert_filings(conn, [
            Filing(
                ticker="SNTS", issuer_name="SONATEL", doc_type="rapport_activites",
                period_kind="H1", period_year=2024, source="brvm_org",
                source_url="https://brvm.org/SNTS/2024-H1.pdf", url_hash="h-h1",
                file_path="p", size_bytes=1, sha256="a", page_count=1,
            ),
            _mk_filing(year=2024),  # annual
        ])
        by_kind = {r["period_kind"]: int(r["id"]) for r in conn.execute(
            "SELECT id, period_kind FROM filings"
        ).fetchall()}
        fin_repo.replace_period(
            conn, filing_id=by_kind["H1"],
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2024, period_kind="H1", revenue=500_000,
            ),
        )
        fin_repo.replace_period(
            conn, filing_id=by_kind["annual"],
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2024, period_kind="annual", revenue=1_000_000,
            ),
        )

    refs = svc.get_financials_source_filings("SNTS")
    assert [(r.period_year, r.period_kind) for r in refs] == [
        (2024, "annual"),
        (2024, "H1"),
    ]


def test_get_financials_source_filings_empty_when_no_extractions(monkeypatch, tmp_path):
    _db, svc = _setup(monkeypatch, tmp_path, n_filings=0)
    assert svc.get_financials_source_filings("SNTS") == []


def test_extract_pending_persists_cash_flow_columns(monkeypatch, tmp_path):
    """End-to-end: a cash-flow-bearing extract lands in the `financials`
    table with the three new columns populated."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=1)
    payload = _happy() | {
        "cash_flow_ops": 400_000_000,
        "capex": 150_000_000,
        "free_cash_flow": 250_000_000,
    }
    client = FakeAnthropic([_json_reply(payload)])

    counts = svc.extract_pending(client=client, project_root=tmp_path)
    assert counts.extracted == 1

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT cash_flow_ops, capex, free_cash_flow FROM financials"
        ).fetchone()
    assert row["cash_flow_ops"] == 400_000_000
    assert row["capex"] == 150_000_000
    assert row["free_cash_flow"] == 250_000_000


def test_reset_missing_cashflow_unstamps_annual_rows_without_cashflow(monkeypatch, tmp_path):
    """The Phase-7 backfill: a `financials` row extracted before the
    cash-flow prompt landed will have `cash_flow_ops` / `capex` /
    `free_cash_flow` all NULL. Clear `extracted_utc` on the filing so
    the next extraction pass picks it up again."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=0)
    with connect(db_path) as conn:
        filings_repo.upsert_filings(conn, [_mk_filing(year=2024), _mk_filing(year=2023)])
        by_year = {int(r["period_year"]): int(r["id"]) for r in conn.execute(
            "SELECT id, period_year FROM filings"
        ).fetchall()}
        # 2024: extracted with cash-flow populated → should stay stamped.
        filings_repo.mark_extracted(conn, by_year[2024])
        fin_repo.replace_period(
            conn, filing_id=by_year[2024],
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2024, revenue=1_000_000,
                cash_flow_ops=100_000, capex=30_000, free_cash_flow=70_000,
            ),
        )
        # 2023: extracted before Phase 7 → cash-flow columns all NULL.
        filings_repo.mark_extracted(conn, by_year[2023])
        fin_repo.replace_period(
            conn, filing_id=by_year[2023],
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2023, revenue=900_000,
            ),
        )

    counts = svc.reset_missing_cashflow()
    assert counts == {"filings_reset": 1, "dry_run": 0}

    with connect(db_path) as conn:
        stamps = {int(r["id"]): r["extracted_utc"] for r in conn.execute(
            "SELECT id, extracted_utc FROM filings"
        ).fetchall()}
    # 2023 filing is now unstamped → will re-enter the next extraction pass.
    assert stamps[by_year[2023]] is None
    # 2024 filing (which has cash-flow data) stays stamped.
    assert stamps[by_year[2024]] is not None


def test_reset_missing_cashflow_second_run_is_a_no_op(monkeypatch, tmp_path):
    """F-25: a filing whose cash-flow statement is structurally missing
    (past the 120k-char truncation or absent) will re-extract to NULL
    forever. The tried-and-failed stamp keeps the recovery pass from
    re-billing ~30-50k tokens per filing on every scheduled run."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=0)
    with connect(db_path) as conn:
        filings_repo.upsert_filings(conn, [_mk_filing(year=2023)])
        fid = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        filings_repo.mark_extracted(conn, fid)
        fin_repo.replace_period(
            conn, filing_id=fid,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2023, revenue=900_000,
            ),
        )

    first = svc.reset_missing_cashflow()
    assert first["filings_reset"] == 1

    # Simulate the re-extraction pass: extracted_utc gets stamped again
    # by the worker, but cash-flow columns remain NULL (structurally
    # missing from the PDF). Without the F-25 stamp the recovery query
    # would pick this filing up again and re-bill it every run.
    with connect(db_path) as conn:
        filings_repo.mark_extracted(conn, fid)

    second = svc.reset_missing_cashflow()
    assert second["filings_reset"] == 0
    # And the stamp survived — the filing is out of the recovery queue
    # for good until an operator clears the column manually.
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT cashflow_recovery_attempted_utc FROM filings WHERE id = ?",
            (fid,),
        ).fetchone()
    assert row["cashflow_recovery_attempted_utc"] is not None


def test_reset_missing_cashflow_dry_run_reports_without_touching(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=0)
    with connect(db_path) as conn:
        filings_repo.upsert_filings(conn, [_mk_filing(year=2023)])
        fid = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        filings_repo.mark_extracted(conn, fid)
        fin_repo.replace_period(
            conn, filing_id=fid,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2023, revenue=900_000,
            ),
        )

    counts = svc.reset_missing_cashflow(dry_run=True)
    assert counts == {"filings_reset": 1, "dry_run": 1}
    with connect(db_path) as conn:
        stamped = list(conn.execute(
            "SELECT 1 FROM filings WHERE extracted_utc IS NOT NULL"
        ).fetchall())
    assert len(stamped) == 1  # untouched


def test_reset_missing_cashflow_skips_interim_rows(monkeypatch, tmp_path):
    """Interim reports (H1/Q1/Q3) rarely publish a full cash-flow
    statement — recovering them would waste extraction budget on rows
    that will never light up cash-flow columns."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=0)
    with connect(db_path) as conn:
        filings_repo.upsert_filings(conn, [
            Filing(
                ticker="SNTS", issuer_name="SONATEL", doc_type="rapport_activites",
                period_kind="H1", period_year=2025, source="brvm_org",
                source_url="u", url_hash="h",
                file_path="p", size_bytes=1, sha256="a", page_count=1,
            ),
        ])
        fid = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        filings_repo.mark_extracted(conn, fid)
        fin_repo.replace_period(
            conn, filing_id=fid,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2025, period_kind="H1", revenue=500_000,
            ),
        )

    counts = svc.reset_missing_cashflow()
    assert counts["filings_reset"] == 0


def test_reset_shadowed_extractions_dry_run_touches_nothing(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_filings=0)
    with connect(db_path) as conn:
        filings_repo.upsert_filings(conn, [
            _mk_filing_of("rapport_annuel"),
            _mk_filing_of("etats_financiers"),
        ])
        both = {r["doc_type"]: r["id"] for r in conn.execute(
            "SELECT id, doc_type FROM filings"
        ).fetchall()}
        for fid in both.values():
            filings_repo.mark_extracted(conn, fid)
        fin_repo.replace_period(
            conn,
            filing_id=both["etats_financiers"],
            financials=fin_repo.FinancialsRow(ticker="SNTS", period_year=2025),
        )

    counts = svc.reset_shadowed_extractions(dry_run=True)
    assert counts == {"periods_shadowed": 1, "filings_reset": 1, "dry_run": 1}
    with connect(db_path) as conn:
        stamped = list(conn.execute(
            "SELECT 1 FROM filings WHERE extracted_utc IS NOT NULL"
        ).fetchall())
    assert len(stamped) == 2
