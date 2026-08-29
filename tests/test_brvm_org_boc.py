from datetime import date

import pytest

from brvm.sources.brvm_org import (
    parse_boc_pdf_date,
    parse_boc_rows,
    resolve_boc_pdf_url,
)


def _read(fixtures_dir, rel: str) -> str:
    return (fixtures_dir / rel).read_text(encoding="utf-8")


class TestResolveBocPdfUrl:
    def test_prefers_english(self, fixtures_dir):
        url = resolve_boc_pdf_url(_read(fixtures_dir, "brvm_org/boc_landing.html"), lang="eng")
        assert url is not None
        assert url.startswith("https://www.brvm.org/")
        assert "boc_eng_" in url
        assert url.endswith(".pdf")

    def test_returns_none_on_empty(self):
        assert resolve_boc_pdf_url("<html></html>") is None


class TestParseBocPdfDate:
    def test_extracts_date_from_filename(self):
        assert parse_boc_pdf_date("boc_eng_20260818_2.pdf") == date(2026, 8, 18)


@pytest.mark.pdf
def test_boc_pdf_readable(fixtures_dir):
    """Smoke test: pypdf should be able to open the captured BOC PDF and
    extract at least some text. The exact table parsing is intentionally
    deferred until we have enough evidence the layout is stable.
    """
    from pypdf import PdfReader

    pdfs = list((fixtures_dir / "brvm_org").glob("boc_*.pdf"))
    if not pdfs:
        pytest.skip("no BOC PDF fixture present")
    reader = PdfReader(str(pdfs[0]))
    assert len(reader.pages) >= 1
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    # Sanity: the PDF should mention BRVM and at least one well-known ticker.
    assert "BRVM" in text.upper()


@pytest.mark.pdf
class TestParseBocRows:
    """Phase 8f: extract (ticker, close, previous, change_pct) rows from
    the BOC PDF so we can cross-check `daily_bars.close` against the
    authoritative source. All figures cross-referenced against the 2026-
    08-18 BOC page 2."""

    def _rows(self, fixtures_dir):
        pdfs = list((fixtures_dir / "brvm_org").glob("boc_*.pdf"))
        if not pdfs:
            pytest.skip("no BOC PDF fixture present")
        return parse_boc_rows(pdfs[0].read_bytes())

    def test_extracts_enough_rows(self, fixtures_dir):
        rows = self._rows(fixtures_dir)
        # The BRVM had 47 listed equities as of 2026-08-18. Multi-line
        # company names skip some rows in pypdf's text stream; the
        # parser is designed to be strict-on-format so we expect at
        # least 25 clean rows.
        assert len(rows) >= 25

    def test_unlc_row_matches_page(self, fixtures_dir):
        # UNLC (Unilever CI) was the biggest loser at -6.88% on 2026-08-18
        # closing at 54,000 from 57,990 previous.
        rows = self._rows(fixtures_dir)
        by_ticker = {r.ticker: r for r in rows}
        assert "UNLC" in by_ticker
        assert by_ticker["UNLC"].close == 54_000.0
        assert by_ticker["UNLC"].previous == 57_990.0
        assert by_ticker["UNLC"].change_pct == -6.88

    def test_ciec_row_matches_page(self, fixtures_dir):
        # CIEC (CIE CI) was the top gainer at +7.43%, closing at 6,360
        # from 5,920 previous.
        rows = self._rows(fixtures_dir)
        by_ticker = {r.ticker: r for r in rows}
        assert "CIEC" in by_ticker
        assert by_ticker["CIEC"].close == 6_360.0
        assert by_ticker["CIEC"].change_pct == 7.43

    def test_no_duplicate_tickers(self, fixtures_dir):
        # A row we can't parse cleanly is dropped rather than mis-parsed;
        # any ticker that does appear should be unique.
        rows = self._rows(fixtures_dir)
        tickers = [r.ticker for r in rows]
        assert len(tickers) == len(set(tickers))

    def test_close_prices_are_positive(self, fixtures_dir):
        rows = self._rows(fixtures_dir)
        assert all(r.close > 0 for r in rows)

    def test_tel_board_rows_are_extracted(self, fixtures_dir):
        """F-04: the TEL (Télécoms) board carries SNTS, ORAC, and ONTBF
        — the highest-turnover tickers on the exchange. The prior
        whitelist omitted TEL, silently dropping these rows from every
        reconciliation cycle even though pypdf extracts them cleanly."""
        rows = self._rows(fixtures_dir)
        by = {r.ticker: r for r in rows}
        # Values cross-referenced against the 2026-08-18 BOC page 2.
        assert "SNTS" in by
        assert by["SNTS"].close == 32_500.0
        assert by["SNTS"].previous == 31_900.0
        assert "ORAC" in by
        assert by["ORAC"].close == 19_000.0
        assert "ONTBF" in by
        assert by["ONTBF"].close == 2_900.0
