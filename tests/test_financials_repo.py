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


def test_replace_period_preserves_prior_segments_when_new_extract_is_empty(tmp_db_path):
    """A period is often covered by two filings on brvm.org: an
    `etats_financiers` (bare financial statements — no shareholders, no
    segments) and a `rapport_annuel` (full annual report with both).
    Whichever one lands second must NOT wipe the other's segment /
    ownership rows just because its own extract came back empty.

    Reproduces the ORAC 2025 bug: rapport_annuel extracted first,
    etats_financiers extracted second, and the second overwrite dropped
    every shareholder row. The Ownership tab then rendered "No data"
    even though the extractor had found shareholders on the first pass.
    """
    filing_id = _seed(tmp_db_path)
    with connect(tmp_db_path) as conn:
        # First filing (rapport_annuel-shape): populates everything.
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2024,
                revenue=1_500_000_000, net_income=300_000_000,
            ),
            segments=[
                fin_repo.SegmentRow(name="Sénégal", segment_kind="geo", share_pct=60.0),
                fin_repo.SegmentRow(name="Mobile", segment_kind="business", share_pct=70.0),
            ],
            ownership=[
                fin_repo.OwnershipRow(holder="SONATEL SA", share_pct=42.3),
                fin_repo.OwnershipRow(holder="Flottant", share_pct=57.7),
            ],
        )

        # Second filing (etats_financiers-shape): refreshes the P&L numbers
        # but returns empty segments + ownership. Prior rows must survive.
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2024,
                revenue=1_600_000_000, net_income=310_000_000,
            ),
            segments=[],
            ownership=[],
        )

        rows = fin_repo.list_financials(conn, "SNTS")
        assert len(rows) == 1
        # P&L numbers reflect the second (newer) extract.
        assert rows[0]["revenue"] == 1_600_000_000
        assert rows[0]["net_income"] == 310_000_000
        # Segments and ownership survived from the first extract.
        segs = fin_repo.list_segments(conn, "SNTS", 2024)
        assert {s["name"] for s in segs} == {"Sénégal", "Mobile"}
        owns = fin_repo.list_ownership(conn, "SNTS", 2024)
        assert {o["holder"] for o in owns} == {"SONATEL SA", "Flottant"}


def test_replace_period_preserves_non_null_scalars_across_extracts(tmp_db_path):
    """F-07 regression: a poorer second filing for the same period must not
    NULL out scalar fields that a richer earlier filing already populated.

    Sonatel-shaped repro: the rapport_annuel carries the full P&L +
    dividend + cash-flow columns; a follow-on ``resultats`` release for
    the same year only fills revenue + operating income. Without
    preserve-non-null, the second extract would blank EPS, dividend, and
    cash-flow — the exact symptom the audit flagged."""
    filing_id = _seed(tmp_db_path)
    with connect(tmp_db_path) as conn:
        # Rich first extract: everything set.
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2024,
                revenue=1_500_000_000,
                operating_income=400_000_000,
                net_income=300_000_000,
                total_assets=5_000_000_000,
                total_equity=2_000_000_000,
                eps=1200.0,
                dividend_per_share=500.0,
                cash_flow_ops=350_000_000,
                capex=120_000_000,
                free_cash_flow=230_000_000,
            ),
        )
        # Poorer second extract: only revenue + operating income; every
        # other scalar comes in as None. Must not clobber the prior values.
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS", period_year=2024,
                revenue=1_600_000_000,
                operating_income=420_000_000,
            ),
        )

        rows = fin_repo.list_financials(conn, "SNTS")
        assert len(rows) == 1
        # Non-null scalars in the second extract win — the newer read is
        # authoritative for the fields it actually reports.
        assert rows[0]["revenue"] == 1_600_000_000
        assert rows[0]["operating_income"] == 420_000_000
        # Null scalars in the second extract preserve the first's values.
        assert rows[0]["net_income"] == 300_000_000
        assert rows[0]["total_assets"] == 5_000_000_000
        assert rows[0]["total_equity"] == 2_000_000_000
        assert rows[0]["eps"] == 1200.0
        assert rows[0]["dividend_per_share"] == 500.0
        assert rows[0]["cash_flow_ops"] == 350_000_000
        assert rows[0]["capex"] == 120_000_000
        assert rows[0]["free_cash_flow"] == 230_000_000


def test_replace_period_new_nonempty_segments_still_replace_prior(tmp_db_path):
    """The preserve rule only kicks in when the new extract is empty. A
    non-empty new list must still fully replace the old one — the
    extractor's newer read is authoritative."""
    filing_id = _seed(tmp_db_path)
    with connect(tmp_db_path) as conn:
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(ticker="SNTS", period_year=2024, revenue=100),
            segments=[fin_repo.SegmentRow(name="Old", segment_kind="business", share_pct=100)],
            ownership=[fin_repo.OwnershipRow(holder="Old", share_pct=100)],
        )
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(ticker="SNTS", period_year=2024, revenue=200),
            segments=[fin_repo.SegmentRow(name="New", segment_kind="business", share_pct=100)],
            ownership=[fin_repo.OwnershipRow(holder="New", share_pct=100)],
        )
        segs = fin_repo.list_segments(conn, "SNTS", 2024)
        assert {s["name"] for s in segs} == {"New"}
        owns = fin_repo.list_ownership(conn, "SNTS", 2024)
        assert {o["holder"] for o in owns} == {"New"}


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
