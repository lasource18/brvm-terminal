"""SQLite repository for the `filings` table (Phase 4a).

Insert-only from the ingest side; Phase 4b will `UPDATE ... SET
extracted_utc = ..., is_scanned = ...` after a successful extraction.
Dedupe on `url_hash` so re-polling a stable brvm.org PDF URL never
re-downloads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from brvm.clock import utc_iso
from brvm.models import Filing


def upsert_filings(conn: sqlite3.Connection, items: Iterable[Filing]) -> tuple[int, int]:
    """Insert new filings keyed on `url_hash`. Existing rows are left
    alone (an in-place UPDATE would clobber the 4b extractor's stamps).

    Returns (inserted, skipped_dupes).
    """
    now = utc_iso()
    inserted = 0
    skipped = 0
    for f in items:
        cur = conn.execute(
            """
            INSERT INTO filings
                (ticker, issuer_name, doc_type, period_kind, period_year,
                 period_label, source, source_url, url_hash, published_date,
                 file_path, size_bytes, sha256, page_count, fetched_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url_hash) DO NOTHING
            """,
            (
                f.ticker,
                f.issuer_name,
                f.doc_type,
                f.period_kind,
                f.period_year,
                f.period_label,
                f.source,
                f.source_url,
                f.url_hash,
                f.published_date.isoformat() if f.published_date else None,
                f.file_path,
                f.size_bytes,
                f.sha256,
                f.page_count,
                now,
            ),
        )
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    return inserted, skipped


def exists_url_hash(conn: sqlite3.Connection, url_hash: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM filings WHERE url_hash = ?", (url_hash,)).fetchone()
        is not None
    )


def list_by_ticker(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    doc_type: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    where = ["ticker = ?"]
    params: list[object] = [ticker]
    if doc_type:
        where.append("doc_type = ?")
        params.append(doc_type)
    params.append(limit)
    return list(
        conn.execute(
            f"""
            SELECT * FROM filings
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(published_date, fetched_utc) DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    )


def list_needing_extraction(
    conn: sqlite3.Connection,
    *,
    doc_types: tuple[str, ...] = (
        "etats_financiers",
        "rapport_annuel",
        "rapport_activites",
    ),
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Rows the extractor hasn't processed yet.

    Default doc_types cover the annual + interim universe (Phase 4c). The
    period_kind on each row tells the caller whether an item is annual
    (`annual`) or interim (`H1`/`Q1`/`Q3`); the extractor's prompt handles
    both. `rapport_activites` is where interim BOA-style reports live —
    excluding it left ~60 filings unreachable through the pipeline.
    """
    placeholders = ",".join("?" * len(doc_types))
    return list(
        conn.execute(
            f"""
            SELECT * FROM filings
            WHERE extracted_utc IS NULL
              AND doc_type IN ({placeholders})
              AND (is_scanned IS NULL OR is_scanned = 0)
            ORDER BY COALESCE(published_date, fetched_utc) DESC, id DESC
            LIMIT ?
            """,
            (*doc_types, limit),
        ).fetchall()
    )


def count_all(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]


def mark_extracted(
    conn: sqlite3.Connection,
    filing_id: int,
    *,
    is_scanned: bool | None = None,
    commit: bool = True,
) -> None:
    """Stamp `extracted_utc` (and optionally `is_scanned`).

    Always called after an extraction attempt — even a failed one — so
    the 4b worker never bills the same PDF twice. `is_scanned=True`
    tells the next pass to skip this filing entirely (see
    `list_needing_extraction`'s `is_scanned = 0` guard)."""
    from brvm.clock import utc_iso

    if is_scanned is None:
        conn.execute(
            "UPDATE filings SET extracted_utc = ? WHERE id = ?",
            (utc_iso(), filing_id),
        )
    else:
        conn.execute(
            "UPDATE filings SET extracted_utc = ?, is_scanned = ? WHERE id = ?",
            (utc_iso(), 1 if is_scanned else 0, filing_id),
        )
    if commit:
        conn.commit()


# --------------------------------------------------------------------------
# OCR bookkeeping (Phase 4c)
# --------------------------------------------------------------------------


def list_pending_ocr(
    conn: sqlite3.Connection,
    *,
    max_pages: int | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    """Filings whose text extraction hit the scanned-PDF wall (4b) and
    haven't been through OCR yet (4c).

    `max_pages` caps how big a file we're willing to OCR — a 400-page
    scanned RSE annex would eat the entire nightly slot on its own.
    Rows with `page_count IS NULL` (pypdf couldn't probe) are included
    conservatively; the caller handles the failure downstream.
    """
    where = ["is_scanned = 1", "ocr_attempted_utc IS NULL"]
    params: list[object] = []
    if max_pages is not None:
        where.append("(page_count IS NULL OR page_count <= ?)")
        params.append(max_pages)
    params.append(limit)
    return list(
        conn.execute(
            f"""
            SELECT * FROM filings
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(published_date, fetched_utc) DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    )


def count_pending_ocr(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM filings WHERE is_scanned = 1 AND ocr_attempted_utc IS NULL"
    ).fetchone()[0]


def apply_ocr_success(
    conn: sqlite3.Connection,
    filing_id: int,
    *,
    size_bytes: int,
    sha256: str,
    page_count: int | None,
    commit: bool = True,
) -> None:
    """OCR succeeded: refresh the file's byte-level attributes, flip
    `is_scanned=0`, and clear `extracted_utc` so the row re-enters
    `list_needing_extraction` on the next pass."""
    from brvm.clock import utc_iso

    conn.execute(
        """
        UPDATE filings
        SET is_scanned = 0,
            ocr_attempted_utc = ?,
            extracted_utc = NULL,
            size_bytes = ?,
            sha256 = ?,
            page_count = COALESCE(?, page_count)
        WHERE id = ?
        """,
        (utc_iso(), size_bytes, sha256, page_count, filing_id),
    )
    if commit:
        conn.commit()


def apply_ocr_failure(
    conn: sqlite3.Connection,
    filing_id: int,
    *,
    commit: bool = True,
) -> None:
    """OCR failed (timeout / bad exit / no output). Stamp
    `ocr_attempted_utc` so the same file isn't retried every night; an
    operator can clear it manually to force a retry after e.g. upgrading
    tesseract."""
    from brvm.clock import utc_iso

    conn.execute(
        "UPDATE filings SET ocr_attempted_utc = ? WHERE id = ?",
        (utc_iso(), filing_id),
    )
    if commit:
        conn.commit()
