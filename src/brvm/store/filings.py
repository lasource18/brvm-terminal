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
    doc_types: tuple[str, ...] = ("etats_financiers", "rapport_annuel"),
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Rows the 4b extractor hasn't processed yet.

    Filtered to annual-ish document types by default — that's the
    universe 4b targets (~50 filings/year across the whole exchange).
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
