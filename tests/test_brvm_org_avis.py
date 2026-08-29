"""Fixture-driven tests for the brvm.org avis-feed parser."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brvm.sources import brvm_org_avis as avis

FIXTURE = Path(__file__).parent / "fixtures" / "brvm_org" / "avis_landing.html"


def _load() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class TestParseAvisPage:
    def test_extracts_admission_rows_with_pdfs(self):
        rows = avis.parse_avis_page(_load())
        assert len(rows) > 0
        # Every row keeps a PDF URL rooted at brvm.org.
        for r in rows:
            assert r.pdf_url.startswith("https://www.brvm.org/")
            assert r.pdf_url.lower().endswith(".pdf")

    def test_multi_ticker_admission_row_lifts_both_tickers(self):
        rows = avis.parse_avis_page(_load())
        # 2026-08-27 avis: "Résultats de première cotation - ETAT DU MALI
        # 6,55 % 2026-2036 (EOM.O23) - ETAT DU MALI 6,35 % 2026-2033" —
        # the second bond's ticker is only in the filename slug.
        eom_row = next(r for r in rows if "EOM.O23" in r.tickers)
        assert eom_row.is_admission
        assert "EOM.O23" in eom_row.tickers
        assert "EOM.O24" in eom_row.tickers
        assert eom_row.published_date == date(2026, 8, 27)

    def test_paren_style_title_ticker_is_lifted(self):
        rows = avis.parse_avis_page(_load())
        # 2026-08-24 TPBF row uses `(TPBF.O26)` style in the title.
        tpbf_row = next(r for r in rows if "TPBF.O26" in r.tickers)
        assert tpbf_row.is_admission
        assert "TPBF.O27" in tpbf_row.tickers

    def test_non_admission_rows_stay_flagged_false(self):
        rows = avis.parse_avis_page(_load())
        # The dividend-calendar and holiday avis are on the fixture but
        # aren't admission notices — they should not be pinned as
        # prospectus surrogates.
        non_admissions = [r for r in rows if not r.is_admission]
        assert non_admissions, "fixture should include non-admission rows"
        # No ticker gets pinned by a non-admission row in the backfill
        # (the caller filters on `is_admission`), so a row about a bond
        # that only mentions the ticker for a coupon-fixing must not
        # slip through.
        for r in non_admissions:
            assert "premiere_cotation" not in r.pdf_url.lower()

    def test_coupon_fixing_row_carries_ticker_but_not_admission_flag(self):
        """`fixation_du_taux_dinteret_trimestriel_tpci.o77.pdf` mentions
        TPCI.O77 but is a quarterly rate-fixing avis, not the admission
        notice. The parser must extract the ticker (for future uses)
        while keeping `is_admission=False` so the backfill script's
        filter drops the row."""
        rows = avis.parse_avis_page(_load())
        tpci_o77_rows = [r for r in rows if "TPCI.O77" in r.tickers]
        assert tpci_o77_rows, "TPCI.O77 should be lifted from the filename"
        assert all(not r.is_admission for r in tpci_o77_rows)


class TestParseLastPageIndex:
    def test_returns_last_page_number_from_pager(self):
        # The live avis feed has ~188 pages at capture time; the fixture
        # should carry a `pager-last` link with a page= query param.
        idx = avis.parse_last_page_index(_load())
        assert idx is not None and idx > 0
