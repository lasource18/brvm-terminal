"""brvm.org — official exchange site.

Phase 1 usage: resolve today's Bulletin Officiel de la Cote (BOC) PDF URL
from the landing page and fetch it. Full PDF table extraction is
inherently fragile, so it's a cross-check for `daily_bars`, not the
primary EOD source. See CLAUDE.md open questions.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import httpx

from brvm.sources._http import make_client

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


def fetch_boc_pdf(client: httpx.Client | None = None, lang: str = "eng") -> bytes | None:
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
        return pdf_r.content
    finally:
        if close:
            client.close()
