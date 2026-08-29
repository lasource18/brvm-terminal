"""brvm.org — avis d'opérations (PR-H follow-up primary source).

The `/fr/marche/avis-et-publications/avis` endpoint is the exchange's
official notices feed. Every bond admission publishes a `Première
cotation` avis (and a follow-up `Résultats de première cotation`) with
a linked PDF that formally announces the listing — the closest thing
brvm.org exposes to a per-bond prospectus. This module walks the feed
and lifts the (ticker, PDF URL) pairs so the bond overview can render
a first-class "Prospectus" link instead of relying on the empty
news_items seed the 0016 migration originally shipped with.

The full note d'information itself is not hosted on brvm.org — it
lives on the lead-arranger's site or on CREPMF. The admission avis is
the best public substitute and is unambiguously tied to a specific
listing.

Parsers are pure (HTML in → dataclasses out) and pagination is
metadata-driven off the Drupal pager so a source redesign trips a
fixture test rather than a runtime crash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx
from selectolax.parser import HTMLParser

from brvm.sources._http import make_client

SOURCE_NAME = "brvm_org"
BASE = "https://www.brvm.org"
AVIS_PATH = "/fr/marche/avis-et-publications/avis"

# Filename-side ticker: brvm.org lowercases the ticker in the slug and
# keeps the dot (`eom.o23`, `tpci.o36`, `bidc.o4`). Two-to-six letters
# before `.o` covers every currently-listed bond.
_FILENAME_TICKER_RE = re.compile(r"([a-z]{2,6}\.o\d{1,3})")

# Title-side ticker: `(EOM.O23)` — the canonical uppercase form. Some
# older titles carry the ticker as a bare token without parens.
_TITLE_TICKER_RE = re.compile(r"\b([A-Z]{2,6}\.O\d{1,3})\b")

# Only pin admission avis — first-listing or first-listing-results.
# Everything else (dividend calendars, coupon rate fixings, holidays,
# radiations) references bonds without being the admission notice.
_ADMISSION_HINTS: tuple[str, ...] = ("premiere_cotation",)

# `<li class="pager-last"><a href="...?page=N">` on every paginated
# page — the trailing pager link goes straight to the last page. Same
# shape as the filings-index pager.
_PAGER_LAST_HREF_RE = re.compile(r"[?&]page=(\d+)")

# Issue #49 title-side spec: "ETAT DU MALI 6,55 % 2026-2036" — issuer
# tokens, coupon (comma or period decimal), issue year, maturity
# year. U+2013 en-dash is accepted alongside `-`. Multi-issuer titles
# yield multiple captures because we finditer.
_TITLE_SPEC_RE = re.compile(
    r"([A-Z][A-Z0-9'.\-\s]+?)\s+(\d{1,2}(?:[.,]\d{1,3})?)\s*%\s+"
    r"(\d{4})\s*[-\u2013]\s*(\d{4})"
)

# Issue #49 filename-side spec: brvm.org drops the decimal separator
# in the coupon (`_tpci_585_2014-2021_`); the coupon is always 3-4
# digits. Split as `<issuer_slug>_<coupon>_<iy>-<my>`; the issuer slug
# is a stretch of one-or-more underscore-separated lowercase tokens.
_FILENAME_SPEC_RE = re.compile(
    r"_((?:[a-z]+(?:'[a-z]+)?_)+)"      # issuer slug, trailing underscore
    r"(\d{3,4})_"                        # coupon x100 (e.g. 585 → 5.85)
    r"(\d{4})-(\d{4})"                   # issue-maturity year range
)

# Slug tokens that appear in filenames but aren't part of an issuer
# name — mostly bond-type prefixes ("social bond", "diaspora bond",
# "gender bond"). We strip these so `_social_bond_crrh_uemoa_600_...`
# resolves against `CRRH-UEMOA`, not `SOCIAL BOND CRRH-UEMOA`.
_FILENAME_ISSUER_STRIPS: tuple[str, ...] = (
    "premiere_cotation_",
    "resultats_de_premiere_cotation_",
    "resultat_de_premiere_cotation_",
    "social_bond_",
    "gender_bond_",
    "diaspora_bond_",
    "diaspora_bonds_",
    "gss_baobab_",
    "keur_samba_",
    "et_",
)


@dataclass(frozen=True)
class BondSpec:
    """A `(issuer, coupon%, issue_year, maturity_year)` triple lifted
    from an avis title or filename. Feeds the coupon+years matcher
    that resolves specs to bond tickers when the avis itself doesn't
    embed the ticker code — the case for older/matured bonds like
    TPCI.O18 whose 2014 admission avis pre-dates the ticker-in-
    filename convention.

    `issuer_brand` is uppercased and stripped of bond-type prefixes so
    downstream SQL can `WHERE UPPER(issuer_name) LIKE '%<brand>%'`.
    """

    issuer_brand: str
    coupon_pct: float
    issue_year: int
    maturity_year: int


@dataclass(frozen=True)
class Avis:
    title: str
    pdf_url: str
    published_date: date | None
    tickers: tuple[str, ...]  # canonical uppercase, e.g. ("EOM.O23", "EOM.O24")
    is_admission: bool
    specs: tuple[BondSpec, ...] = ()  # fallback when tickers is empty


def _parse_date_iso(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _extract_tickers(title: str, pdf_url: str) -> tuple[str, ...]:
    """Return the ordered set of bond tickers referenced by one row.

    Title first (canonical uppercase, `(EOM.O23)`), filename second
    (`_eom.o23_` — lowercase, uppercased here). Deduped in insertion
    order so multi-ticker avis surface every ticker exactly once.
    """
    seen: dict[str, None] = {}
    for m in _TITLE_TICKER_RE.finditer(title):
        seen.setdefault(m.group(1), None)
    for m in _FILENAME_TICKER_RE.finditer(pdf_url.lower()):
        seen.setdefault(m.group(1).upper(), None)
    return tuple(seen.keys())


def _is_admission(pdf_url: str) -> bool:
    lower = pdf_url.lower()
    return any(h in lower for h in _ADMISSION_HINTS)


def _clean_title_issuer(raw: str) -> str:
    """Normalise a title-side issuer capture. Strips leading bond-type
    prefixes (SOCIAL BOND, GENDER BOND, DIASPORA BONDS, KEUR SAMBA)
    and stray dash-punctuation left over from title separators."""
    up = raw.upper().strip(" -\u2013")
    for prefix in ("SOCIAL BOND ", "GENDER BOND ",
                   "DIASPORA BONDS ", "DIASPORA BOND ",
                   "KEUR SAMBA ", "GSS BAOBAB ", "GSS "):
        if up.startswith(prefix):
            up = up[len(prefix):]
    return up.strip()


def _clean_filename_issuer(raw: str) -> str:
    """Convert `etat_du_mali_` (trailing underscore) → `ETAT DU MALI`,
    stripping bond-type prefixes and separator tokens along the way."""
    s = raw
    for junk in _FILENAME_ISSUER_STRIPS:
        s = s.replace(junk, "")
    # Drop the trailing separator underscore left after the coupon
    # match consumed the numeric block.
    return s.strip("_").replace("_", " ").upper().strip()


def _extract_specs(title: str, pdf_url: str) -> tuple[BondSpec, ...]:
    """Return the `(issuer, coupon, iy, my)` triples referenced by one
    row — deduped in insertion order.

    Older admission avis (~pre-2019) don't embed the ticker in either
    title or filename; the audit's blocker case TPCI.O18 (a 2014-2021
    bond) falls into that bucket. Extracting the spec lets the
    downstream matcher resolve it against `securities` on `(issuer
    LIKE brand, coupon_rate ≈, maturity_year =)`.
    """
    seen: dict[tuple[str, float, int, int], None] = {}
    for m in _TITLE_SPEC_RE.finditer(title):
        issuer = _clean_title_issuer(m.group(1))
        if not issuer or len(issuer) < 2:
            continue
        try:
            coupon = float(m.group(2).replace(",", "."))
        except ValueError:
            continue
        iy, my = int(m.group(3)), int(m.group(4))
        if my < iy or my - iy > 40:
            continue
        seen.setdefault((issuer, coupon, iy, my), None)
    # Filename path: the coupon is decimal-stripped (`585` = 5.85).
    for m in _FILENAME_SPEC_RE.finditer(pdf_url.lower()):
        issuer = _clean_filename_issuer(m.group(1))
        if not issuer or len(issuer) < 2:
            continue
        raw = m.group(2)
        # Two conventions in the wild: `585` (3-digit) → 5.85 and
        # `0575` (4-digit leading zero) → 5.75. Divide 3-digit by 100,
        # 4-digit by 100 as well since the extra zero encodes the
        # tenths not-yet-published coupon.
        coupon = int(raw) / 100.0
        iy, my = int(m.group(3)), int(m.group(4))
        if my < iy or my - iy > 40:
            continue
        seen.setdefault((issuer, coupon, iy, my), None)
    return tuple(
        BondSpec(
            issuer_brand=k[0], coupon_pct=k[1],
            issue_year=k[2], maturity_year=k[3],
        )
        for k in seen
    )


def parse_avis_page(html: str) -> list[Avis]:
    """Parse one avis-listing page.

    Rows without a PDF href are skipped silently — brvm.org occasionally
    ships an empty download cell for an avis whose PDF is still being
    uploaded. The parser stays strict on the row shape so a source
    redesign shows up as a fixture-test regression.
    """
    tree = HTMLParser(html)
    out: list[Avis] = []
    for tr in tree.css("table tbody tr"):
        title_cell = tr.css_first("td.views-field-title")
        file_cell = tr.css_first("td.views-field-field-fichier-avis")
        if title_cell is None or file_cell is None:
            continue
        title = title_cell.text(strip=True)
        anchor = file_cell.css_first("a")
        if anchor is None:
            continue
        href = anchor.attributes.get("href") or ""
        if not href.lower().endswith(".pdf"):
            continue
        pdf_url = href if href.startswith("http") else f"{BASE}{href}"

        date_cell = tr.css_first("td.views-field-field-date-avis span")
        published = _parse_date_iso(
            date_cell.attributes.get("content") if date_cell else None
        )

        tickers = _extract_tickers(title, pdf_url)
        specs = _extract_specs(title, pdf_url)
        out.append(
            Avis(
                title=title,
                pdf_url=pdf_url,
                published_date=published,
                tickers=tickers,
                is_admission=_is_admission(pdf_url),
                specs=specs,
            )
        )
    return out


def parse_last_page_index(html: str) -> int | None:
    """Return the highest 0-indexed page number advertised by the pager.

    None means the feed fits on a single page (unusual — the live feed
    has 188+ pages of history — but a shrunk fixture triggers this)."""
    tree = HTMLParser(html)
    node = tree.css_first("li.pager-last a")
    if node is None:
        return None
    href = node.attributes.get("href") or ""
    m = _PAGER_LAST_HREF_RE.search(href)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def fetch_avis_page(
    page: int = 0, client: httpx.Client | None = None
) -> tuple[list[Avis], int | None]:
    """Fetch one page of the avis feed. Returns (rows, last_page_index).

    `page=0` is the newest set — the pager on that page also carries the
    total, so a caller walking history knows how many more pages remain.
    """
    close = client is None
    client = client or make_client()
    url = f"{BASE}{AVIS_PATH}" if page == 0 else f"{BASE}{AVIS_PATH}?page={page}"
    try:
        r = client.get(url)
        r.raise_for_status()
        html = r.text
        return parse_avis_page(html), parse_last_page_index(html)
    finally:
        if close:
            client.close()
