"""brvm.org — official exchange site.

Phase 1 usage: resolve today's Bulletin Officiel de la Cote (BOC) PDF URL
from the landing page and fetch it. Phase 8f adds row-level extraction
of the equity-market table so we can cross-check `daily_bars.close`
against the authoritative exchange bulletin. Full PDF table extraction
is inherently fragile — we keep the row parser narrowly focused on
(ticker, close, previous, change_pct) since those four columns are
what the reconciliation needs; wider extraction lives as a follow-up.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from kodji.sources._http import make_client
from kodji.sources._num import parse_en_number

SOURCE_NAME = "brvm_org"
BASE = "https://www.brvm.org"

_BOC_HREF_RE = re.compile(
    r'href="([^"]+/boc_(fr|eng)_(\d{8})(?:_\d+)?\.pdf)"', re.IGNORECASE
)


def resolve_boc_pdf_url(landing_html: str, lang: str = "eng") -> str | None:
    """Return the absolute URL of today's BOC PDF from the landing HTML.

    `lang` is "eng" or "fr". If both are present, prefer the requested lang;
    return None if no matching href is found.
    """
    lang = lang.lower()
    candidates: list[tuple[str, str, str]] = _BOC_HREF_RE.findall(landing_html)
    if not candidates:
        return None
    # Prefer the requested language; fall back to any.
    filtered = [c for c in candidates if c[1].lower() == lang] or candidates
    href = filtered[0][0]
    return href if href.startswith("http") else f"{BASE}{href}"


def parse_boc_pdf_date(filename: str) -> date | None:
    """Extract the session date from a BOC filename like boc_eng_20260818_2.pdf."""
    m = re.search(r"_(\d{8})", filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


# ---------- fetchers ----------


@dataclass(frozen=True)
class BocFetch:
    """Result of a BOC pull. `session_date` is what the PDF filename
    encodes — the authoritative day the bulletin covers, which is what
    reconciliation must compare against rather than the local
    `daily_bars.MAX(session_date)` (F-04)."""

    pdf_bytes: bytes
    session_date: date | None


def fetch_boc(
    client: httpx.Client | None = None, lang: str = "eng"
) -> BocFetch | None:
    """Resolve today's BOC PDF URL and download the bytes, returning the
    session date embedded in the filename alongside. Returns None when
    the landing page doesn't advertise a matching PDF (weekends /
    holidays / a source outage) so the caller can degrade cleanly."""
    close = client is None
    client = client or make_client()
    try:
        r = client.get(f"{BASE}/en/marche/bulletin-officiel-de-la-cote")
        r.raise_for_status()
        url = resolve_boc_pdf_url(r.text, lang=lang)
        if url is None:
            return None
        pdf_r = client.get(url)
        pdf_r.raise_for_status()
        return BocFetch(
            pdf_bytes=pdf_r.content,
            session_date=parse_boc_pdf_date(url),
        )
    finally:
        if close:
            client.close()


def fetch_boc_pdf(client: httpx.Client | None = None, lang: str = "eng") -> bytes | None:
    """Backwards-compatible bytes-only wrapper around `fetch_boc`."""
    f = fetch_boc(client=client, lang=lang)
    return f.pdf_bytes if f else None


# ---------- BOC row extraction --------------------------------------------


@dataclass(frozen=True)
class BocRow:
    """One equity-market row extracted from the BOC PDF's central table.

    `close` is what we cross-check `daily_bars.close` against;
    `previous` and `change_pct` come along because they're on the same
    row and the reconciliation report benefits from surfacing all three
    when a mismatch fires. Other columns from the PDF (volume, value,
    reference price, YTD, dividend info) are intentionally not
    extracted here — pypdf's text stream splits multi-line company
    names across lines and getting those five fields reliable would
    triple the parser's surface area.
    """

    ticker: str
    close: float
    previous: float | None = None
    change_pct: float | None = None


# The equity market table on pages 2-3 uses 4-letter tickers (occasionally
# 5 with a country suffix, e.g. BOABF, ONTBF, BOABF, ETIT). Each row
# starts on a fresh line with a board prefix, then the ticker. `_ROW_RE`
# captures rows where pypdf kept all core columns on one line — the
# wider "close previous change" trio. Rows that wrapped (long company
# names split across a text-stream break) are skipped rather than
# mis-parsed; the tickers we care about most for reconciliation (the
# ones with real volume) reliably fit on one line.
#
# F-04: `TEL` (Télécoms) was missing from the whitelist, silently
# dropping SNTS, ORAC, and ONTBF — the highest-turnover names — from
# every reconciliation cycle even though their BOC rows parse cleanly.
_BOC_BOARD_RE = re.compile(r"^(?:CB|CD|ENE|FIN|IND|SPU|TEL)\s+")
_BOC_TICKER_RE = re.compile(r"^([A-Z]{3,5})\s+")

# `<number> <number> <number> <±number> %` — previous / open / close /
# change%. Numbers use `,` for thousands and `.` for decimals in the
# English BOC.
_NUM = r"[\d,]+(?:\.\d+)?"
_BOC_PRICES_RE = re.compile(
    rf"(?P<previous>{_NUM})\s+(?P<open>{_NUM})\s+(?P<close>{_NUM})\s+"
    rf"(?P<change>-?{_NUM})\s*%"
)


def parse_boc_rows(pdf_bytes: bytes) -> list[BocRow]:
    """Extract one `BocRow` per equity found in the BOC PDF.

    Skips the summary / index / palmarès pages (page 1) and the
    bond-market section that follows the equity total. A row we can't
    parse cleanly is dropped — the caller reads this as
    "reconciliation-eligible rows" and mismatches on the rest surface
    as absent instead of noisy.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    out: list[BocRow] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m_board = _BOC_BOARD_RE.match(line)
        if not m_board:
            continue
        rest = line[m_board.end():]
        m_tkr = _BOC_TICKER_RE.match(rest)
        if not m_tkr:
            continue
        ticker = m_tkr.group(1)
        if ticker in seen:
            continue
        m_px = _BOC_PRICES_RE.search(rest[m_tkr.end():])
        if m_px is None:
            continue
        try:
            previous = parse_en_number(m_px.group("previous"))
            close = parse_en_number(m_px.group("close"))
            change_pct = parse_en_number(m_px.group("change"))
        except ValueError:
            continue
        out.append(BocRow(
            ticker=ticker, close=close,
            previous=previous, change_pct=change_pct,
        ))
        seen.add(ticker)
    return out
