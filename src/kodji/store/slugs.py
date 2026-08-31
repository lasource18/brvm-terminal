"""SQLite repository for `filing_source_slugs` — the persisted
`(source, slug) → ticker` map.

Behaviour:
* `remember(...)` writes even a NULL ticker on purpose, so a slug the
  resolver couldn't map isn't fuzzy-matched again on every poll. A
  manual `UPDATE filing_source_slugs SET ticker='XYZ' WHERE ...`
  overrides that.
* `get(...)` returns the row (or None if we've never seen the slug),
  never raises.
"""

from __future__ import annotations

import sqlite3

from kodji.clock import utc_iso


def get(conn: sqlite3.Connection, source: str, slug: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM filing_source_slugs WHERE source = ? AND slug = ?",
        (source, slug),
    ).fetchone()


def get_ticker(conn: sqlite3.Connection, source: str, slug: str) -> str | None:
    """Return the ticker for a known slug, or None if unmapped / unknown.

    Callers must distinguish "never seen" (use `get()`) from "seen and
    unresolved" — the latter should not trigger a fresh fuzzy match.
    """
    row = get(conn, source, slug)
    return row["ticker"] if row else None


def remember(
    conn: sqlite3.Connection,
    source: str,
    slug: str,
    ticker: str | None,
    *,
    display_name: str | None = None,
    note: str | None = None,
) -> None:
    """Upsert one (source, slug) row. `ticker=None` records an unresolved
    slug so the fuzzy matcher isn't retried on every poll.

    Existing rows are refreshed for `display_name` and `resolved_utc`;
    `ticker` is only overwritten when the new value is non-NULL, so a
    manual override survives an automatic re-resolution attempt.
    `note` is only overwritten when explicitly passed, so a hand-written
    note ('manual override') doesn't get wiped by a scheduled poll.
    """
    conn.execute(
        """
        INSERT INTO filing_source_slugs
            (source, slug, ticker, display_name, resolved_utc, note)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, slug) DO UPDATE SET
            ticker        = COALESCE(excluded.ticker, ticker),
            display_name  = COALESCE(excluded.display_name, display_name),
            resolved_utc  = excluded.resolved_utc,
            note          = COALESCE(excluded.note, note)
        """,
        (source, slug, ticker, display_name, utc_iso(), note),
    )
    conn.commit()


def list_unresolved(conn: sqlite3.Connection, source: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM filing_source_slugs WHERE source = ? AND ticker IS NULL "
            "ORDER BY slug",
            (source,),
        ).fetchall()
    )
