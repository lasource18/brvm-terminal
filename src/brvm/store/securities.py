"""SQLite repository for the `securities` table."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from brvm.clock import utc_iso
from brvm.models import Security


def upsert(conn: sqlite3.Connection, items: Iterable[Security]) -> int:
    """Insert new securities, or refresh name/country/sector/source_url and
    bump `last_seen_utc` for existing ones. Returns rows touched."""
    now = utc_iso()
    n = 0
    for s in items:
        conn.execute(
            """
            INSERT INTO securities
                (ticker, isin, name, kind, country, sector, currency,
                 source_url, first_seen_utc, last_seen_utc, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(ticker) DO UPDATE SET
                name        = excluded.name,
                isin        = COALESCE(excluded.isin, securities.isin),
                country     = COALESCE(excluded.country, securities.country),
                sector      = COALESCE(excluded.sector, securities.sector),
                source_url  = COALESCE(excluded.source_url, securities.source_url),
                last_seen_utc = excluded.last_seen_utc,
                active      = 1
            """,
            (
                s.ticker,
                s.isin,
                s.name,
                s.kind,
                s.country,
                s.sector,
                s.currency,
                s.source_url,
                now,
                now,
            ),
        )
        n += 1
    conn.commit()
    return n


def count(conn: sqlite3.Connection, kind: str | None = None) -> int:
    if kind:
        return conn.execute(
            "SELECT COUNT(*) FROM securities WHERE kind = ?", (kind,)
        ).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM securities").fetchone()[0]


def list_by_kind(conn: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM securities WHERE kind = ? AND active = 1 ORDER BY ticker",
            (kind,),
        ).fetchall()
    )
