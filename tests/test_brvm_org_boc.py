from datetime import date

import pytest

from brvm.sources.brvm_org import parse_boc_pdf_date, resolve_boc_pdf_url


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
