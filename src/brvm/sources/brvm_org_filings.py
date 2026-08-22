"""brvm.org — per-issuer filings pages (Phase 4a primary source).

Two pages are parsed here:

* `/fr/rapports-societes-cotees[?page=N]` — the paginated issuer index.
  Each `<div><a href="/fr/rapports-societe-cotes/SLUG">DISPLAY</a></div>`
  yields one (slug, display_name) pair. brvm.org paginates 30 issuers per
  page; the fetcher walks pages until it sees an empty one.

* `/fr/rapports-societe-cotes/<slug>` — a table of PDF links for one
  issuer. Each row carries `<strong>TICKER : Title</strong>` plus a
  `<a href="...pdf">Télécharger</a>`. brvm.org filenames follow a strict
  pattern (`YYYYMMDD_-_type_-_period_-_ticker_cc.pdf`) so most of the
  structured metadata (published date, doc_type, period_kind, period_year)
  is recovered from the URL rather than from HTML, which is more brittle.

Parsers are pure functions (HTML str -> dicts / dataclasses). Fetchers
wrap them with polite httpx I/O.
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

_INDEX_PATH = "/fr/rapports-societes-cotees"
_ISSUER_PATH_PREFIX = "/fr/rapports-societe-cotes/"

# Slug/name pair on the issuer index. The link may be absolute or relative;
# selectolax normalizes neither, so we match both forms.
_SLUG_ATTR_RE = re.compile(r"/fr/rapports-societe-cotes/([a-z0-9-]+)$")

# Filename shape on the download links. The leading YYYYMMDD is publication
# date; the rest is a snake_case description. Delimiters between blocks are
# `_-_` — brvm.org has been consistent since at least 2023.
_FILENAME_RE = re.compile(
    r"""^
    (?P<yyyymmdd>\d{8})               # publication date
    _-_
    (?P<body>.+?)                     # everything up to the trailing issuer slug
    \.pdf$
    """,
    re.IGNORECASE | re.VERBOSE,
)


# --------------------------------------------------------------------------
# Public dataclasses (kept flat — the store layer promotes these to Filing
# rows after ticker resolution).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuerIndexEntry:
    slug: str
    display_name: str


@dataclass(frozen=True)
class ParsedFiling:
    source_url: str
    file_name: str
    title: str                # the `<strong>` text on the row, verbatim
    issuer_code: str | None   # left of the ':' in the title, e.g. 'SONATEL SN'
    published_date: date | None
    doc_type: str             # one of models.FilingDocType
    period_kind: str | None   # 'annual' | 'H1' | 'Q1' | 'Q3' | 'other' | None
    period_year: int | None
    period_label: str | None  # raw human label from title, e.g. 'Exercice 2025'


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def parse_issuers_index(html: str) -> list[IssuerIndexEntry]:
    """Return every (slug, display_name) pair on one index page.

    The same slug appearing twice on the same page (a Drupal quirk we've
    seen elsewhere on brvm.org) is deduped in insertion order.
    """
    tree = HTMLParser(html)
    seen: dict[str, IssuerIndexEntry] = {}
    for a in tree.css("a"):
        href = a.attributes.get("href") or ""
        m = _SLUG_ATTR_RE.search(href)
        if not m:
            continue
        slug = m.group(1)
        if slug in seen:
            continue
        name = a.text(strip=True)
        if not name:
            continue
        seen[slug] = IssuerIndexEntry(slug=slug, display_name=name)
    return list(seen.values())


def parse_issuer_page(html: str) -> list[ParsedFiling]:
    """Return every filing row on one issuer page.

    Rows without a PDF href are skipped silently — brvm.org occasionally
    ships an empty row for a report that's announced but not uploaded yet.
    """
    tree = HTMLParser(html)
    out: list[ParsedFiling] = []
    for row in tree.css("tr"):
        strong = row.css_first("strong")
        title = strong.text(strip=True) if strong else ""
        anchor = None
        for a in row.css("a"):
            href = a.attributes.get("href") or ""
            if href.lower().endswith(".pdf"):
                anchor = a
                break
        if anchor is None:
            continue
        href = anchor.attributes.get("href") or ""
        url = href if href.startswith("http") else f"{BASE}{href}"

        issuer_code = None
        if ":" in title:
            issuer_code = title.split(":", 1)[0].strip()

        file_name = url.rsplit("/", 1)[-1]
        meta = _parse_filename(file_name)
        label = _period_label_from_title(title)
        out.append(
            ParsedFiling(
                source_url=url,
                file_name=file_name,
                title=title,
                issuer_code=issuer_code,
                published_date=meta["published_date"],
                doc_type=meta["doc_type"],
                period_kind=meta["period_kind"] or _period_kind_from_label(label),
                period_year=meta["period_year"] or _period_year_from_label(label),
                period_label=label,
            )
        )
    return out


# --------------------------------------------------------------------------
# Filename / title parsing helpers (pure functions, exercised by tests)
# --------------------------------------------------------------------------


# The order here matters — a filename can match several patterns
# ("rapport_dactivites_annuel" contains "rapport_dactivites") so we test
# more specific labels first.
_DOC_TYPE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"rapport[_ ]d?activites?[_ ]annuel", re.I), "rapport_annuel"),
    (re.compile(r"rapport[_ ]annuel", re.I), "rapport_annuel"),
    (re.compile(r"etats[_ ]financiers", re.I), "etats_financiers"),
    (re.compile(r"rapport[_ ]d?activites?", re.I), "rapport_activites"),
    (re.compile(r"resultats?", re.I), "resultats"),
    # `\b` treats `_` as a word character, so `\brse\b` misses `_rse_` in
    # snake_case filenames. Use explicit letter/digit lookarounds instead.
    (re.compile(r"(?<![A-Za-z0-9])rse(?![A-Za-z0-9])", re.I), "rse"),
    (re.compile(r"(assemblee|convocation|resolution|pouvoir|procuration)", re.I), "assemblee"),
)


def _classify_doc_type(text: str) -> str:
    for pat, label in _DOC_TYPE_PATTERNS:
        if pat.search(text):
            return label
    return "autre"


_PERIOD_ANNUAL_RE = re.compile(r"exercice[_\s-]*(\d{4})", re.I)
_PERIOD_H1_RE = re.compile(r"1er[_\s-]*semestre[_\s-]*(\d{4})", re.I)
_PERIOD_Q1_RE = re.compile(r"1er[_\s-]*trimestre[_\s-]*(\d{4})", re.I)
_PERIOD_Q3_RE = re.compile(r"3(?:eme|ème|e)[_\s-]*trimestre[_\s-]*(\d{4})", re.I)
# Fallback: "etats_financiers_2025" (bare year) implies annual for that doc
# type. Applied only when nothing more specific matched. Underscore-safe
# boundary (see _DOC_TYPE_PATTERNS for why `\b` doesn't work here).
_BARE_YEAR_RE = re.compile(r"(?<![A-Za-z0-9])(20\d{2})(?![A-Za-z0-9])")


def _parse_filename(file_name: str) -> dict:
    """Return {'published_date', 'doc_type', 'period_kind', 'period_year'}."""
    published_date: date | None = None
    doc_type = "autre"
    period_kind: str | None = None
    period_year: int | None = None

    m = _FILENAME_RE.match(file_name)
    body = ""
    if m:
        try:
            published_date = datetime.strptime(m.group("yyyymmdd"), "%Y%m%d").date()
        except ValueError:
            published_date = None
        body = m.group("body")

    text = body or file_name
    doc_type = _classify_doc_type(text)
    period_kind, period_year = _classify_period(text)
    return {
        "published_date": published_date,
        "doc_type": doc_type,
        "period_kind": period_kind,
        "period_year": period_year,
    }


def _classify_period(text: str) -> tuple[str | None, int | None]:
    if m := _PERIOD_H1_RE.search(text):
        return "H1", int(m.group(1))
    if m := _PERIOD_Q1_RE.search(text):
        return "Q1", int(m.group(1))
    if m := _PERIOD_Q3_RE.search(text):
        return "Q3", int(m.group(1))
    if m := _PERIOD_ANNUAL_RE.search(text):
        return "annual", int(m.group(1))
    # "etats_financiers_2025" — no period keyword, but a bare year in the
    # filename is almost always the fiscal year.
    if m := _BARE_YEAR_RE.search(text):
        return "annual", int(m.group(1))
    return None, None


def _period_label_from_title(title: str) -> str | None:
    """Best-effort human label pulled from the display title after the ':'."""
    if ":" not in title:
        return None
    right = title.split(":", 1)[1].strip()
    # Titles often carry "... - Exercice 2025" or "... - 1er semestre 2026";
    # grab the trailing tail after the last dash if present.
    for sep in (" - ", " \u2013 ", " \u2014 "):  # ASCII hyphen, en dash, em dash
        if sep in right:
            right = right.rsplit(sep, 1)[1].strip()
            break
    return right or None


def _period_kind_from_label(label: str | None) -> str | None:
    if not label:
        return None
    return _classify_period(label)[0]


def _period_year_from_label(label: str | None) -> int | None:
    if not label:
        return None
    return _classify_period(label)[1]


# --------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------


def fetch_issuers_index(
    client: httpx.Client | None = None, *, max_pages: int = 10
) -> list[IssuerIndexEntry]:
    """Walk `/fr/rapports-societes-cotees` across pages until empty.

    `max_pages` is a defence against a broken 'Next' link — the real page
    count as of 2026-08 is 3.
    """
    close = client is None
    client = client or make_client()
    try:
        out: dict[str, IssuerIndexEntry] = {}
        for page in range(max_pages):
            url = f"{BASE}{_INDEX_PATH}" + (f"?page={page}" if page else "")
            r = client.get(url)
            r.raise_for_status()
            page_entries = parse_issuers_index(r.text)
            new = [e for e in page_entries if e.slug not in out]
            if not new:
                # Empty or fully-seen page -> we've walked past the end.
                break
            for e in new:
                out[e.slug] = e
        return list(out.values())
    finally:
        if close:
            client.close()


def fetch_issuer_filings(
    slug: str, client: httpx.Client | None = None
) -> list[ParsedFiling]:
    close = client is None
    client = client or make_client()
    try:
        r = client.get(f"{BASE}{_ISSUER_PATH_PREFIX}{slug}")
        r.raise_for_status()
        return parse_issuer_page(r.text)
    finally:
        if close:
            client.close()
