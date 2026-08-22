"""Tests for store/financials.py: replace_period + read helpers."""

from __future__ import annotations

from datetime import date

from brvm.db import connect
from brvm.models import Filing, Security
from brvm.store import filings as filings_repo
from brvm.store import financials as fin_repo
from brvm.store import securities as sec_repo

from .conftest import apply_migrations


def _mk_filing(ticker: str = "SNTS", year: int = 2024) -> Filing:
    return Filing(
        ticker=ticker,
        issuer_name="SONATEL",
        doc_type="rapport_annuel",
        period_kind="annual",
        period_year=year,
        source="brvm_org",
        source_url=f"https://brvm.org/{ticker}/{year}.pdf",
        url_hash=f"hash-{ticker}-{year}",
        published_date=date(year + 1, 3, 15),
        file_path=f"data/filings/{ticker}/{year}.pdf",
        size_bytes=123456,
        sha256="deadbeef",
        page_count=42,
    )


def _seed(db_path) -> int:
    """Insert a security + one filing, return the filing_id."""
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN")])
        filings_repo.upsert_filings(conn, [_mk_filing()])
        return int(conn.execute("SELECT id FROM filings").fetchone()["id"])


def test_replace_period_inserts_a_full_triple(tmp_db_path):
    filing_id = _seed(tmp_db_path)
    with connect(tmp_db_path) as conn:
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS",
                period_year=2024,
                revenue=1_500_000_000,
                net_income=300_000_000,
            ),
            segments=[
                fin_repo.SegmentRow(name="Sénégal", segment_kind="geo", share_pct=60.0),
                fin_repo.SegmentRow(name="Mali", segment_kind="geo", share_pct=40.0),
            ],
            ownership=[
                fin_repo.OwnershipRow(holder="SONATEL SA", share_pct=42.3),
                fin_repo.OwnershipRow(holder="Flottant", share_pct=57.7),
            ],
        )

        rows = fin_repo.list_financials(conn, "SNTS")
        assert len(rows) == 1
        assert rows[0]["revenue"] == 1_500_000_000
        segs = fin_repo.list_segments(conn, "SNTS", 2024)
        assert {s["name"] for s in segs} == {"Sénégal", "Mali"}
        owns = fin_repo.list_ownership(conn, "SNTS", 2024)
        assert {o["holder"] for o in owns} == {"SONATEL SA", "Flottant"}


def test_replace_period_overwrites_prior_run(tmp_db_path):
    filing_id = _seed(tmp_db_path)
    with connect(tmp_db_path) as conn:
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(ticker="SNTS", period_year=2024, revenue=100),
            segments=[fin_repo.SegmentRow(name="Old", segment_kind="business", share_pct=100)],
            ownership=[fin_repo.OwnershipRow(holder="Old Holder", share_pct=100)],
        )
        # Re-run — same period key, different content.
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(ticker="SNTS", period_year=2024, revenue=999),
            segments=[fin_repo.SegmentRow(name="New", segment_kind="business", share_pct=100)],
            ownership=[fin_repo.OwnershipRow(holder="New Holder", share_pct=100)],
        )

        rows = fin_repo.list_financials(conn, "SNTS")
        assert len(rows) == 1 and rows[0]["revenue"] == 999
        segs = fin_repo.list_segments(conn, "SNTS", 2024)
        assert {s["name"] for s in segs} == {"New"}
        owns = fin_repo.list_ownership(conn, "SNTS", 2024)
        assert {o["holder"] for o in owns} == {"New Holder"}


def test_replace_period_dedupes_segments_and_owners_within_a_batch(tmp_db_path):
    filing_id = _seed(tmp_db_path)
    with connect(tmp_db_path) as conn:
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(ticker="SNTS", period_year=2024),
            segments=[
                fin_repo.SegmentRow(name="Autres", segment_kind="business", share_pct=5),
                fin_repo.SegmentRow(name="Autres", segment_kind="business", share_pct=7),
            ],
            ownership=[
                fin_repo.OwnershipRow(holder="Flottant", share_pct=50),
                fin_repo.OwnershipRow(holder="Flottant", share_pct=51),
            ],
        )
        assert len(fin_repo.list_segments(conn, "SNTS", 2024)) == 1
        assert len(fin_repo.list_ownership(conn, "SNTS", 2024)) == 1


def test_list_financials_returns_most_recent_first(tmp_db_path):
    filing_id = _seed(tmp_db_path)
    with connect(tmp_db_path) as conn:
        for y in (2020, 2021, 2022, 2023, 2024):
            fin_repo.replace_period(
                conn,
                filing_id=filing_id,
                financials=fin_repo.FinancialsRow(
                    ticker="SNTS", period_year=y, revenue=1_000_000 * y
                ),
            )
        years = [r["period_year"] for r in fin_repo.list_financials(conn, "SNTS", limit=3)]
        assert years == [2024, 2023, 2022]


def test_mark_extracted_stamps_the_filing(tmp_db_path):
    filing_id = _seed(tmp_db_path)
    with connect(tmp_db_path) as conn:
        filings_repo.mark_extracted(conn, filing_id)
        row = conn.execute("SELECT extracted_utc, is_scanned FROM filings").fetchone()
        assert row["extracted_utc"] is not None
        assert row["is_scanned"] is None

    with connect(tmp_db_path) as conn:
        filings_repo.mark_extracted(conn, filing_id, is_scanned=True)
        row = conn.execute("SELECT extracted_utc, is_scanned FROM filings").fetchone()
        assert row["is_scanned"] == 1
