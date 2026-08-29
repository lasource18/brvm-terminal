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


class TestExtractSpecs:
    """Issue #49: fallback spec extraction for older avis whose title
    and filename don't embed a ticker code. The coupon+years matcher
    downstream keys on `(issuer_brand, coupon%, iy, my)`."""

    def _specs(self, title: str, pdf_url: str):
        return avis._extract_specs(title, pdf_url)

    def test_title_spec_extracts_issuer_coupon_years(self):
        specs = self._specs(
            "Résultats de première cotation - ETAT DU MALI 6,55 % 2026-2036",
            "https://www.brvm.org/sites/default/files/20260827_-_avis.pdf",
        )
        by = {s.issuer_brand: s for s in specs}
        assert "ETAT DU MALI" in by
        s = by["ETAT DU MALI"]
        assert s.coupon_pct == 6.55
        assert s.issue_year == 2026
        assert s.maturity_year == 2036

    def test_filename_spec_recovers_ticker_less_older_avis(self):
        """Blocker case for issue #49: TPCI.O18 (5.85% 2014-2021) has
        an admission avis whose filename lacks the ticker slug. The
        filename spec must still lift the (TPCI, 5.85, 2014, 2021)
        triple."""
        specs = self._specs(
            "Première cotation TPCI",
            "https://www.brvm.org/sites/default/files/"
            "20140301_-_premiere_cotation_-_tpci_585_2014-2021_.pdf",
        )
        by = {s.issuer_brand: s for s in specs}
        assert "TPCI" in by
        s = by["TPCI"]
        assert s.coupon_pct == 5.85
        assert s.issue_year == 2014
        assert s.maturity_year == 2021

    def test_filename_spec_multi_word_issuer(self):
        specs = self._specs(
            "Première cotation",
            "https://www.brvm.org/sites/default/files/"
            "20260430_-_avis_ndeg116_brvmdg_-_premiere_cotation_-_"
            "etat_du_mali_655_2026-2036_eom.o21_et_"
            "etat_du_mali_635_2026-2033_eom.o22_.pdf",
        )
        by = {(s.issuer_brand, s.maturity_year): s for s in specs}
        assert ("ETAT DU MALI", 2036) in by
        assert by[("ETAT DU MALI", 2036)].coupon_pct == 6.55
        assert ("ETAT DU MALI", 2033) in by
        assert by[("ETAT DU MALI", 2033)].coupon_pct == 6.35

    def test_implausible_year_range_dropped(self):
        """Reject specs where maturity is before issue or tenor > 40y
        — guards against a stray digit run mimicking the coupon-years
        shape."""
        specs = self._specs(
            "junk 5% 2030-2020",
            "https://www.brvm.org/dummy.pdf",
        )
        assert specs == ()


class TestParseLastPageIndex:
    def test_returns_last_page_number_from_pager(self):
        # The live avis feed has ~188 pages at capture time; the fixture
        # should carry a `pager-last` link with a page= query param.
        idx = avis.parse_last_page_index(_load())
        assert idx is not None and idx > 0
