"""Filings corpus service (Phase 4a).

Two entry points:

* `pull_all(client=None)` — walks every brvm.org issuer, resolves the
  slug → ticker, downloads any new PDFs to `data/filings/<ticker>/`,
  and records one row per file in `filings`. Idempotent: existing
  `url_hash` rows are skipped, and existing files on disk are left
  alone.

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


def _load_name_index(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Return two indexes:

    * `name_idx`: full-normalized-name → ticker (matches the news module).
    * `root_iso_idx`: (root, iso_country) → ticker (added for brvm.org
      where names carry a suffix like " BN" that sikafinance writes as
      " BENIN" or " BJ").

    Both are keyed via `setdefault` so a duplicate name doesn't quietly
    reassign an earlier ticker.
    """
    name_idx: dict[str, str] = {}
    root_iso_idx: dict[tuple[str, str], str] = {}
    rows = conn.execute(
        "SELECT ticker, name, country FROM securities WHERE kind='equity'"
    )
    for r in rows:
        n = _normalize(r["name"])
        name_idx.setdefault(n, r["ticker"])
        root, iso = _split_root_country(n)
        if not iso and r["country"]:
            iso = _ALIAS_TO_ISO.get(_normalize(r["country"]))
        if iso and root:
            root_iso_idx.setdefault((root, iso), r["ticker"])
    return name_idx, root_iso_idx


def _fuzzy_resolve(
    name_idx: dict[str, str],
    root_iso_idx: dict[tuple[str, str], str],
    display_name: str,
) -> str | None:
    """Match a display name against `securities`. Tries, in order:
    exact full-name; (root, ISO country) index; bidirectional starts-with
    over the full-name index (kept for the news-shape 'SGBCI' vs
    'SOCIETE GENERALE CI' cases).
    """
    key = _normalize(display_name)
    if not key:
        return None
    if hit := name_idx.get(key):
        return hit
    root, iso = _split_root_country(display_name)
    if iso and (hit := root_iso_idx.get((root, iso))):
        return hit
    for full, tk in name_idx.items():
        if full.startswith(key) or key.startswith(full):
            return tk
    return None


def resolve_ticker(
    conn: sqlite3.Connection,
    source: str,
    slug: str,
    display_name: str,
    *,
    indexes: tuple[dict[str, str], dict[tuple[str, str], str]] | None = None,
) -> str | None:
    """Look up (source, slug) → ticker. Persists the outcome so a fuzzy
    match runs once per slug, not once per poll.

    Returns None if the slug can't be mapped to a known security. A
    subsequent call with the same slug will hit the persisted NULL and
    return None immediately (the operator can override it manually via
    `UPDATE filing_source_slugs SET ticker=...`).
    """
    known = slugs_repo.get(conn, source, slug)
    if known is not None:
        return known["ticker"]

    name_idx, root_iso_idx = indexes or _load_name_index(conn)
    ticker = _fuzzy_resolve(name_idx, root_iso_idx, display_name)
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


def _safe_file_name(published_date: str | None, doc_type: str, period_label: str | None,
                    url: str) -> str:
    """Predictable filename that stays readable in `ls`.

    Falls back to the URL basename if we can't reconstruct anything better.
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
    return f"{stem}.pdf"


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
    delay_between_requests_s: float = 0.5,
) -> dict[str, int]:
    """Walk every brvm.org issuer, download new filings, persist rows.

    Idempotent: the `url_hash` UNIQUE constraint on `filings` guarantees
    we never re-insert, and the file-exists check under `data/filings/`
    means an already-downloaded PDF isn't re-fetched even if the row was
    deleted for some reason. `max_issuers` lets `just filings-pull` do a
    quick smoke run.
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
