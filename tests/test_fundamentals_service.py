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
