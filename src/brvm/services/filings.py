"""Filings corpus service (Phase 4a + 4c).

Entry points:

* `pull_all(client=None)` — walks every brvm.org issuer, resolves the
  slug → ticker, downloads any new PDFs to `data/filings/<ticker>/`,
  and records one row per file in `filings`. Idempotent: existing
  `url_hash` rows are skipped, and existing files on disk are left
  alone.

* `promote_from_communiques(client=None)` — Phase 4c fallback. Scans
  `news_items[kind=communique]` from sikafinance, promotes rows whose
  title matches a financial-filing pattern (états financiers / rapport
  d'activités / rapport annuel), and downloads the PDF into the same
  corpus. Cross-source dedupe against brvm.org means we don't fetch the
  same period twice when both sources carry it.

* `resolve_ticker(source, slug, display_name, *, conn)` — the
  slug-map lookup. First checks `filing_source_slugs` (persisted map);
  on miss, tries fuzzy name matching against `securities.name`; the
  outcome (ticker or NULL) is written back so the fuzzy path runs
  once per slug, not once per poll.

No LLM calls, no PDF text extraction — that's Phase 4b.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import httpx

from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.models import Filing
from brvm.sources import brvm_org_filings
from brvm.sources._dedupe import url_hash as compute_url_hash
from brvm.sources._http import make_client
from brvm.store import filings as filings_repo
from brvm.store import slugs as slugs_repo

log = get(__name__)

_WS_RE = re.compile(r"\s+")
# Punctuation that is safe to drop when comparing an issuer display name
# against a securities.name row (Bolloré/BOLLORE-style accents included).
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


# --------------------------------------------------------------------------
# Ticker resolution
# --------------------------------------------------------------------------


# brvm.org uses its own two-letter country codes on issuer suffixes, and
# sikafinance names use a mix of two-letter codes and expanded country names.
# The two disagree in two places (Benin: BN vs BJ; Niger: NG vs NE), so we
# normalize both sides to ISO 3166-1 alpha-2 before comparing.
_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "BJ": ("BJ", "BN", "BENIN"),
    "BF": ("BF", "BURKINA FASO", "BURKINA"),
    "CI": ("CI", "COTE D IVOIRE", "COTE DIVOIRE", "IVORY COAST"),
    "ML": ("ML", "MALI"),
    "NE": ("NE", "NG", "NIGER"),
    "SN": ("SN", "SENEGAL"),
    "TG": ("TG", "TOGO"),
}
_ALIAS_TO_ISO: dict[str, str] = {
    alias: iso for iso, aliases in _COUNTRY_ALIASES.items() for alias in aliases
}


def _normalize(s: str) -> str:
    s = _WS_RE.sub(" ", s.replace("\xa0", " ")).strip().upper()
    return _PUNCT_RE.sub(" ", s).strip()


def _split_root_country(name: str) -> tuple[str, str | None]:
    """Return (root, iso_country_or_None) after peeling a trailing country
    token off the display name.

    Tries progressively-longer trailing runs so "BANK OF AFRICA BURKINA
    FASO" and "BANK OF AFRICA BF" both collapse to root="BANK OF AFRICA",
    iso="BF".
    """
    tokens = _normalize(name).split()
    if not tokens:
        return "", None
    for take in (3, 2, 1):
        if take > len(tokens):
            continue
        candidate = " ".join(tokens[-take:])
        if candidate in _ALIAS_TO_ISO:
            root = " ".join(tokens[:-take]).strip()
            return root or candidate, _ALIAS_TO_ISO[candidate]
    return " ".join(tokens), None


# Manual brvm.org slug → ticker overrides for issuers the fuzzy matcher
# can't reach. These are typically rename cases (BOLLORE → AGL), missing-
# space cases (PALM CI vs PALMCI), or abbreviated slugs whose display name
# doesn't share tokens with the sikafinance-based `securities.name`
# (BICI CI vs BICICI, LNB vs LOTERIE NATIONALE DU BENIN).
#
# Add here rather than as SQL overrides so the mapping stays in code
# review and travels with the repo; the operator can still override via
# `UPDATE filing_source_slugs SET ticker=…` for one-off exceptions.
_MANUAL_SLUG_ALIASES: dict[str, str] = {
    # brvm.org slug (lowercase, hyphenated)      → ticker in securities
    "bici-ci":                          "BICC",   # BICICI
    "bollore-transport-logistics":      "SDSC",   # renamed to Africa Global Logistics
    "cfao-motors-ci":                   "CFAC",   # CFAO CI
    "ecobank-tg":                       "ETIT",   # ETI TG
    "lnb":                              "LNBB",   # Loterie Nationale du Bénin
    "palm-ci":                          "PALC",   # PALMCI (no space)
    "sgb-ci":                           "SGBC",   # SGBCI (no space)
    "sib":                              "SIBC",   # Société Ivoirienne de Banque
    "totalenergies-marketing-sn":       "TTLS",   # TOTAL SENEGAL
    "tractafric-ci":                    "PRSC",   # Tractafric Motors CI
}


@dataclass(frozen=True)
class _NameIndex:
    """Bundles the three lookup tables the resolver needs. Kept as a small
    dataclass rather than a tuple so a future add (e.g. ISIN) doesn't need
    to sweep every callsite."""

    by_name: dict[str, str]                       # normalized name -> ticker
    by_ticker: dict[str, str]                     # ticker -> ticker (identity)
    by_root_iso: dict[tuple[str, str], str]       # (root, iso country) -> ticker


def _load_name_index(conn: sqlite3.Connection) -> _NameIndex:
    """Build the three lookup tables from `securities`.

    * `by_name` catches display names that match a listed name outright
      ("BANK OF AFRICA BURKINA FASO" → BOABF).
    * `by_ticker` catches slugs whose display name IS the ticker itself
      ("NSBC" → NSBC — brvm.org occasionally shows just the code).
    * `by_root_iso` bridges brvm.org's short country suffixes ("BN"/"NG")
      against sikafinance's ISO alpha-2 ("BJ"/"NE") or expanded names.
    """
    by_name: dict[str, str] = {}
    by_ticker: dict[str, str] = {}
    by_root_iso: dict[tuple[str, str], str] = {}
    rows = conn.execute(
        "SELECT ticker, name, country FROM securities WHERE kind='equity'"
    )
    for r in rows:
        by_name.setdefault(_normalize(r["name"]), r["ticker"])
        by_ticker.setdefault(_normalize(r["ticker"]), r["ticker"])
        root, iso = _split_root_country(_normalize(r["name"]))
        if not iso and r["country"]:
            iso = _ALIAS_TO_ISO.get(_normalize(r["country"]))
        if iso and root:
            by_root_iso.setdefault((root, iso), r["ticker"])
    return _NameIndex(by_name=by_name, by_ticker=by_ticker, by_root_iso=by_root_iso)


def _fuzzy_resolve(indexes: _NameIndex, display_name: str) -> str | None:
    """Match a display name against `securities`. Tries, in order:
    exact full-name; ticker match (for brvm.org rows whose display name
    IS the ticker, e.g. 'NSBC'); (root, ISO country) index; bidirectional
    starts-with over the full-name index (kept for the news-shape 'SGBCI'
    vs 'SOCIETE GENERALE CI' cases).
    """
    key = _normalize(display_name)
    if not key:
        return None
    if hit := indexes.by_name.get(key):
        return hit
    if hit := indexes.by_ticker.get(key):
        return hit
    root, iso = _split_root_country(display_name)
    if iso and (hit := indexes.by_root_iso.get((root, iso))):
        return hit
    for full, tk in indexes.by_name.items():
        if full.startswith(key) or key.startswith(full):
            return tk
    return None


def resolve_ticker(
    conn: sqlite3.Connection,
    source: str,
    slug: str,
    display_name: str,
    *,
    indexes: _NameIndex | None = None,
) -> str | None:
    """Look up (source, slug) → ticker. Persists the outcome so a fuzzy
    match runs once per slug, not once per poll.

    Resolution order:
    1. Persisted `filing_source_slugs` row (with or without a ticker)
    2. `_MANUAL_SLUG_ALIASES` — hand-mapped renames + slug-vs-name
       mismatches that the fuzzy matcher can't reach
    3. Fuzzy match against `securities`

    Returns None if the slug can't be mapped to a known security. A
    subsequent call with the same slug will hit the persisted NULL and
    return None immediately (the operator can override it manually via
    `UPDATE filing_source_slugs SET ticker=...`).
    """
    known = slugs_repo.get(conn, source, slug)
    if known is not None and known["ticker"] is not None:
        # Persisted resolution wins.
        return known["ticker"]

    ticker: str | None = None
    if source == "brvm_org":
        ticker = _MANUAL_SLUG_ALIASES.get(slug)
    if ticker is None and known is None:
        # F-26: only run the O(n) fuzzy pass the FIRST time we see a
        # slug. On subsequent polls a persisted NULL means "already
        # tried, didn't match" — the alias table is still consulted
        # above so a fresh `_MANUAL_SLUG_ALIASES` entry can still
        # rescue previously-unresolved slugs, but the fuzzy scan
        # against every equity name doesn't need to re-run every 5
        # minutes forever.
        idx = indexes or _load_name_index(conn)
        ticker = _fuzzy_resolve(idx, display_name)

    # Only remember when we actually resolved (or on the very first
    # attempt for this slug). A persisted NULL that still doesn't
    # resolve is left untouched — no need to bump `first_seen`.
    if ticker is not None or known is None:
        slugs_repo.remember(
            conn,
            source,
            slug,
            ticker,
            display_name=display_name,
            note=None if ticker else "auto: no securities.name match",
        )
    if ticker is None:
        log.info("filings: unresolved slug source=%s slug=%s name=%r", source, slug, display_name)
    return ticker


# --------------------------------------------------------------------------
# Downloader
# --------------------------------------------------------------------------


def _filings_root() -> Path:
    p = Path(settings.filings_root)
    return p if p.is_absolute() else Path.cwd() / p


def _size_ok(size_bytes: int) -> bool:
    return size_bytes <= settings.extract_max_pdf_mb * 1024 * 1024


# macOS APFS and Linux ext4 both cap filename bytes at 255. brvm.org's
# period labels are occasionally a full CAC sentence (see the SAPH 2024
# "rapport des commissaires aux comptes sur l'existence…" row) and blow
# through that when NFC-encoded. Cap the stem at 180 chars so a `.pdf`
# extension plus an optional `_deadbeef` collision suffix still fits under
# 200 bytes even in worst-case UTF-8.
_MAX_STEM_CHARS = 180


def _truncate_stem(stem: str, url: str) -> str:
    """Keep filenames under the FS byte cap. When a stem exceeds
    `_MAX_STEM_CHARS`, keep the head and append a short hash of the URL so
    two long-labelled filings with different sources don't collide."""
    if len(stem) <= _MAX_STEM_CHARS:
        return stem
    tag = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    # Reserve room for the "_<tag>" suffix so the total stays bounded.
    head = stem[: _MAX_STEM_CHARS - len(tag) - 1].rstrip("_-")
    return f"{head}_{tag}"


def _safe_file_name(published_date: str | None, doc_type: str, period_label: str | None,
                    url: str) -> str:
    """Predictable filename that stays readable in `ls`.

    Falls back to the URL basename if we can't reconstruct anything better.
    Long period labels (occasionally a whole CAC sentence on brvm.org) are
    truncated with a URL hash suffix so the filesystem doesn't reject them.
    """
    stem_parts: list[str] = []
    if published_date:
        stem_parts.append(published_date)
    stem_parts.append(doc_type)
    if period_label:
        # Compact for filesystem: "Exercice 2025" -> "exercice-2025".
        stem_parts.append(re.sub(r"\s+", "-", period_label.strip().lower()))
    stem = "_".join(p for p in stem_parts if p)
    if not stem:
        return url.rsplit("/", 1)[-1]
    return f"{_truncate_stem(stem, url)}.pdf"


def _pdf_page_count(path: Path) -> int | None:
    """Return page count, or None if pypdf can't parse (encrypted, corrupt)."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dep is pinned
        return None
    try:
        reader = PdfReader(str(path))
        return len(reader.pages)
    except Exception as e:  # pypdf raises a zoo of exception types
        log.warning("pypdf couldn't read %s: %s", path, e)
        return None


def _download_pdf(client: httpx.Client, url: str, dest: Path) -> tuple[int, str] | None:
    """Stream one PDF to `dest`. Returns (size_bytes, sha256_hex) or None
    on any error (logged, not raised — one broken filing doesn't abort the
    pass).

    Enforces the size cap mid-stream: if the accumulated body crosses the
    limit we bail out without keeping the partial file.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    max_bytes = settings.extract_max_pdf_mb * 1024 * 1024
    sha = hashlib.sha256()
    size = 0
    try:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        log.warning("filings: %s exceeds %d MB cap, aborting",
                                    url, settings.extract_max_pdf_mb)
                        # F-29: the earlier revision returned here
                        # without unlinking `tmp`, so aborted oversize
                        # streams left a `.part` file next to their
                        # intended destination — Phase 4a's "never
                        # keeps a partial" contract was broken.
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        return None
                    sha.update(chunk)
                    fh.write(chunk)
    except httpx.HTTPError as e:
        log.warning("filings: HTTP error fetching %s: %s", url, e)
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(dest)
    return size, sha.hexdigest()


def pull_all(
    client: httpx.Client | None = None,
    *,
    max_issuers: int | None = None,
    only_tickers: set[str] | None = None,
    delay_between_requests_s: float = 0.5,
) -> dict[str, int]:
    """Walk every brvm.org issuer, download new filings, persist rows.

    Idempotent: the `url_hash` UNIQUE constraint on `filings` guarantees
    we never re-insert, and the file-exists check under `data/filings/`
    means an already-downloaded PDF isn't re-fetched even if the row was
    deleted for some reason. `max_issuers` lets `just filings-pull` do a
    quick smoke run; `only_tickers` restricts the walk to a specific
    subset (useful for backfilling a single issuer after a fetcher fix
    or manually testing pagination on a well-known slug).
    """
    close = client is None
    client = client or make_client()
    counts = {
        "issuers_seen": 0,
        "issuers_resolved": 0,
        "issuers_unresolved": 0,
        "filings_seen": 0,
        "filings_new": 0,
        "filings_dupe": 0,
        "filings_failed_download": 0,
        "filings_oversize_or_broken": 0,
    }
    try:
        issuers = brvm_org_filings.fetch_issuers_index(client=client)
    except httpx.HTTPError as e:
        log.error("filings: could not fetch issuer index: %s", e)
        if close:
            client.close()
        return counts

    if max_issuers is not None:
        issuers = issuers[:max_issuers]

    db_path = Path(settings.db_path)
    try:
        with connect(db_path) as conn:
            indexes = _load_name_index(conn)
            known_tickers = {
                r[0] for r in conn.execute("SELECT ticker FROM securities").fetchall()
            }

            for entry in issuers:
                counts["issuers_seen"] += 1
                ticker = resolve_ticker(
                    conn, brvm_org_filings.SOURCE_NAME, entry.slug, entry.display_name,
                    indexes=indexes,
                )
                if ticker is None or ticker not in known_tickers:
                    counts["issuers_unresolved"] += 1
                    continue
                if only_tickers is not None and ticker not in only_tickers:
                    # Resolved but filtered out — don't count against
                    # "unresolved", just skip the fetch.
                    continue
                counts["issuers_resolved"] += 1

                try:
                    parsed = brvm_org_filings.fetch_issuer_filings(entry.slug, client=client)
                except httpx.HTTPError as e:
                    log.warning("filings: issuer %s failed: %s", entry.slug, e)
                    continue

                for pf in parsed:
                    counts["filings_seen"] += 1
                    uhash = compute_url_hash(pf.source_url)
                    if filings_repo.exists_url_hash(conn, uhash):
                        counts["filings_dupe"] += 1
                        continue

                    file_name = _safe_file_name(
                        pf.published_date.isoformat() if pf.published_date else None,
                        pf.doc_type,
                        pf.period_label,
                        pf.source_url,
                    )
                    # Different URLs with the same filename get a hash suffix
                    # so the second one doesn't overwrite the first on disk.
                    dest = _filings_root() / ticker / file_name
                    if dest.exists():
                        dest = dest.with_stem(f"{dest.stem}_{uhash[:8]}")

                    got = _download_pdf(client, pf.source_url, dest)
                    if got is None:
                        counts["filings_failed_download"] += 1
                        continue
                    size_bytes, sha256_hex = got
                    if not _size_ok(size_bytes):
                        # Belt-and-braces — should be caught mid-stream.
                        dest.unlink(missing_ok=True)
                        counts["filings_oversize_or_broken"] += 1
                        continue

                    page_count = _pdf_page_count(dest)
                    if page_count is None and size_bytes < 1024:
                        # Almost certainly HTML masquerading as .pdf.
                        dest.unlink(missing_ok=True)
                        counts["filings_oversize_or_broken"] += 1
                        continue

                    # Store the path exactly as constructed — a relative
                    # FILINGS_ROOT stays relative to CWD, an absolute one is
                    # honoured as-is. Callers who need a real path resolve
                    # against `settings.filings_root` themselves.
                    filing = Filing(
                        ticker=ticker,
                        issuer_name=pf.issuer_code or entry.display_name,
                        doc_type=pf.doc_type,  # type: ignore[arg-type]
                        period_kind=pf.period_kind,  # type: ignore[arg-type]
                        period_year=pf.period_year,
                        period_label=pf.period_label,
                        source=brvm_org_filings.SOURCE_NAME,
                        source_url=pf.source_url,
                        url_hash=uhash,
                        published_date=pf.published_date,
                        file_path=str(dest),
                        size_bytes=size_bytes,
                        sha256=sha256_hex,
                        page_count=page_count,
                    )
                    ins, _ = filings_repo.upsert_filings(conn, [filing])
                    counts["filings_new"] += ins

                if delay_between_requests_s:
                    time.sleep(delay_between_requests_s)
    finally:
        if close:
            client.close()

    log.info("filings pull: %s", counts)
    return counts


# --------------------------------------------------------------------------
# Sikafinance-communiqué fallback (Phase 4c)
# --------------------------------------------------------------------------


# Titles worth promoting to the filings corpus. Order matters: the more
# specific label wins. Any communiqué whose title matches none of these is
# left in `news_items` alone (it stays browsable in the news UI).
_COMMUNIQUE_DOC_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # "Rapport annuel" / "Rapport d'activités annuel"
    (re.compile(r"rapport\s+d[e']?\s*activit[eé]s?\s+annuel", re.I), "rapport_annuel"),
    (re.compile(r"rapport\s+annuel", re.I), "rapport_annuel"),
    # États financiers (annual or interim — the period regex tells them apart)
    (re.compile(r"[eé]tats?\s+financiers?", re.I), "etats_financiers"),
    # Rapport d'activités (usually interim; annual variant was matched above)
    (re.compile(r"rapport\s+d[e']?\s*activit[eé]s?", re.I), "rapport_activites"),
    # Auditor limited-review report on interim financials — treat as the
    # interim activity report (same underlying figures land inside).
    (re.compile(r"rapport\s+d[e']?examen\s+limit[eé].*(?:interm[eé]diaires?|semestr|trimestr)", re.I),
     "rapport_activites"),
    # Standalone "Résultats" / "Communiqué de résultats" — headline P&L.
    (re.compile(r"(?:communiqu[eé]\s+(?:de|des)\s+)?r[eé]sultats?\b", re.I), "resultats"),
)

# Period regexes tuned for sikafinance titles (spaces + accents, not
# underscores). Anchored on `\b` because titles are plain French, not
# snake_case filenames.
_TITLE_H1_RE = re.compile(r"\b1er\s+semestre\s+(\d{4})", re.I)
_TITLE_Q1_RE = re.compile(r"\b1er\s+trimestre\s+(\d{4})", re.I)
_TITLE_Q3_RE = re.compile(
    r"\b3(?:e|eme|ème)\s+trimestre\s+(\d{4})", re.I,
)
# Q2 is unusual (usually reported as H1); a "2eme trimestre" gets mapped to
# H1 so it lands in the same slot as the "1er semestre" figures.
_TITLE_Q2_RE = re.compile(
    r"\b2(?:e|eme|ème)\s+trimestre\s+(\d{4})", re.I,
)
_TITLE_ANNUAL_RE = re.compile(r"\bexercice\s+(\d{4})", re.I)


@dataclass(frozen=True)
class ClassifiedCommunique:
    doc_type: str
    period_kind: str | None
    period_year: int | None
    period_label: str | None


def classify_communique_title(title: str) -> ClassifiedCommunique | None:
    """Return a filing classification for a communiqué title, or None when
    the title isn't a fundamentals filing (dividend notice, AGM convocation,
    liquidity-contract report, credit rating, press release, ...).

    Kept as a pure function so the tests can cover the whole taxonomy
    without setting up a DB.
    """
    if not title:
        return None
    doc_type: str | None = None
    for pat, label in _COMMUNIQUE_DOC_PATTERNS:
        if pat.search(title):
            doc_type = label
            break
    if doc_type is None:
        return None

    period_kind: str | None = None
    period_year: int | None = None
    period_label: str | None = None
    if m := _TITLE_H1_RE.search(title):
        period_kind, period_year = "H1", int(m.group(1))
        period_label = f"1er semestre {period_year}"
    elif m := _TITLE_Q2_RE.search(title):
        # Fold Q2 activity reports into H1: they cover the same six months
        # and stopping-short is unusual (BOA reports "1er semestre"; SITAB
        # reports "2eme trimestre"). Merging keeps one row per issuer-semester.
        period_kind, period_year = "H1", int(m.group(1))
        period_label = f"2eme trimestre {period_year}"
    elif m := _TITLE_Q1_RE.search(title):
        period_kind, period_year = "Q1", int(m.group(1))
        period_label = f"1er trimestre {period_year}"
    elif m := _TITLE_Q3_RE.search(title):
        period_kind, period_year = "Q3", int(m.group(1))
        period_label = f"3eme trimestre {period_year}"
    elif m := _TITLE_ANNUAL_RE.search(title):
        period_kind, period_year = "annual", int(m.group(1))
        period_label = f"Exercice {period_year}"
    elif doc_type == "rapport_annuel":
        # A bare "Rapport annuel 2025" without "Exercice" still implies
        # annual; try to salvage a year from the title.
        m = re.search(r"\b(20\d{2})\b", title)
        if m:
            period_kind, period_year = "annual", int(m.group(1))
            period_label = f"Rapport annuel {period_year}"

    return ClassifiedCommunique(
        doc_type=doc_type,
        period_kind=period_kind,
        period_year=period_year,
        period_label=period_label,
    )


def _period_already_covered(
    conn: sqlite3.Connection,
    ticker: str,
    doc_type: str,
    period_kind: str | None,
    period_year: int | None,
) -> bool:
    """Cross-source dedupe: skip download when brvm.org already carries
    the same (ticker, doc_type, period_kind, period_year) triple.

    Returns False for rows we haven't managed to classify a period for —
    those get downloaded so the operator can still find them, keyed by
    url_hash alone.
    """
    if not (period_kind and period_year):
        return False
    row = conn.execute(
        """
        SELECT 1 FROM filings
        WHERE ticker = ?
          AND doc_type = ?
          AND period_kind = ?
          AND period_year = ?
        LIMIT 1
        """,
        (ticker, doc_type, period_kind, period_year),
    ).fetchone()
    return row is not None


def _sikafinance_file_stem(
    published: date | None,
    doc_type: str,
    period_label: str | None,
    *,
    url: str = "",
) -> str:
    """Filesystem-safe filename stem, prefixed so it can't collide with a
    brvm.org filename for the same ticker. Long labels get the same
    truncate-with-URL-hash treatment as `_safe_file_name`."""
    parts: list[str] = ["sikafinance"]
    if published:
        parts.append(published.isoformat())
    parts.append(doc_type)
    if period_label:
        parts.append(re.sub(r"\s+", "-", period_label.strip().lower()))
    return _truncate_stem("_".join(parts), url or doc_type)


def _parse_published(published_at: str | None) -> date | None:
    if not published_at:
        return None
    try:
        return datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def promote_from_communiques(
    client: httpx.Client | None = None,
    *,
    limit: int | None = None,
    delay_between_requests_s: float = 0.5,
) -> dict[str, int]:
    """Turn sikafinance communiqué rows into `filings` entries.

    Reads unattributed communiqués (`kind='communique'`) from
    `news_items`, classifies the title, resolves the issuer to a ticker
    (via the news row's `ticker_hint` first, then a full-name lookup),
    downloads the PDF, and inserts a `filings` row. Rows whose URL is
    already in `filings` (either from a prior promote pass or — via the
    cross-source period check — from brvm.org) are skipped without a
    network call.

    Returns a per-category counter that mirrors `pull_all`'s shape.
    """
    close = client is None
    client = client or make_client()
    counts = {
        "communiques_seen": 0,
        "not_a_filing": 0,
        "unresolved_ticker": 0,
        "cross_source_dupe": 0,
        "url_dupe": 0,
        "filings_new": 0,
        "failed_download": 0,
        "oversize_or_broken": 0,
    }

    db_path = Path(settings.db_path)
    try:
        with connect(db_path) as conn:
            indexes = _load_name_index(conn)
            known_tickers = {
                r[0] for r in conn.execute("SELECT ticker FROM securities").fetchall()
            }

            rows = _list_promotable_communiques(conn, limit=limit)
            for row in rows:
                counts["communiques_seen"] += 1
                classified = classify_communique_title(row["title"])
                if classified is None:
                    counts["not_a_filing"] += 1
                    continue

                ticker = row["ticker_hint"] or _fuzzy_resolve(
                    indexes, row["issuer_name"] or "",
                )
                if ticker is None or ticker not in known_tickers:
                    counts["unresolved_ticker"] += 1
                    continue

                uhash = compute_url_hash(row["url"])
                if filings_repo.exists_url_hash(conn, uhash):
                    counts["url_dupe"] += 1
                    continue

                if _period_already_covered(
                    conn,
                    ticker=ticker,
                    doc_type=classified.doc_type,
                    period_kind=classified.period_kind,
                    period_year=classified.period_year,
                ):
                    counts["cross_source_dupe"] += 1
                    continue

                published = _parse_published(row["published_at"])
                stem = _sikafinance_file_stem(
                    published,
                    classified.doc_type,
                    classified.period_label,
                    url=row["url"],
                )
                dest = _filings_root() / ticker / f"{stem}.pdf"
                if dest.exists():
                    dest = dest.with_stem(f"{dest.stem}_{uhash[:8]}")

                got = _download_pdf(client, row["url"], dest)
                if got is None:
                    counts["failed_download"] += 1
                    continue
                size_bytes, sha256_hex = got
                if not _size_ok(size_bytes):
                    dest.unlink(missing_ok=True)
                    counts["oversize_or_broken"] += 1
                    continue

                page_count = _pdf_page_count(dest)
                if page_count is None and size_bytes < 1024:
                    dest.unlink(missing_ok=True)
                    counts["oversize_or_broken"] += 1
                    continue

                filing = Filing(
                    ticker=ticker,
                    issuer_name=row["issuer_name"],
                    doc_type=classified.doc_type,  # type: ignore[arg-type]
                    period_kind=classified.period_kind,  # type: ignore[arg-type]
                    period_year=classified.period_year,
                    period_label=classified.period_label,
                    source="sikafinance",
                    source_url=row["url"],
                    url_hash=uhash,
                    published_date=published,
                    file_path=str(dest),
                    size_bytes=size_bytes,
                    sha256=sha256_hex,
                    page_count=page_count,
                )
                ins, _ = filings_repo.upsert_filings(conn, [filing])
                counts["filings_new"] += ins

                if delay_between_requests_s:
                    time.sleep(delay_between_requests_s)
    finally:
        if close:
            client.close()

    log.info("filings promote (sikafinance): %s", counts)
    return counts


def _list_promotable_communiques(
    conn: sqlite3.Connection, *, limit: int | None
) -> list[sqlite3.Row]:
    """Communiqué rows we haven't already promoted — a LEFT JOIN on
    `filings.url_hash` filters cheaply without touching Python."""
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    return list(
        conn.execute(
            f"""
            SELECT n.id, n.url, n.title, n.issuer_name, n.ticker_hint,
                   n.published_at
            FROM news_items n
            LEFT JOIN filings f ON f.source_url = n.url
            WHERE n.source = 'sikafinance'
              AND n.kind = 'communique'
              AND f.id IS NULL
            ORDER BY COALESCE(n.published_at, n.fetched_utc) DESC, n.id DESC
            {limit_sql}
            """
        ).fetchall()
    )
