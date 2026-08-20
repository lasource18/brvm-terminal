"""SQLite repositories for quote_snapshots, daily_bars, index_levels, fetch_log."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from brvm.clock import utc_iso
from brvm.models import DailyBar, IndexLevel, Quote


def insert_snapshots(conn: sqlite3.Connection, quotes: Iterable[Quote]) -> int:
    now = utc_iso()
    n = 0
    for q in quotes:
        conn.execute(
            """
            INSERT OR REPLACE INTO quote_snapshots
                (ticker, captured_utc, source, last, prev_close, open, high, low,
                 volume, turnover, change_abs, change_pct, is_stale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                q.ticker,
                now,
                q.source,
                q.last,
                q.prev_close,
                q.open,
                q.high,
                q.low,
                q.volume,
                q.turnover,
                q.change_abs,
                q.change_pct,
                int(q.is_stale),
            ),
        )
        n += 1
    conn.commit()
    return n


def upsert_daily_bars(conn: sqlite3.Connection, bars: Iterable[DailyBar]) -> int:
    now = utc_iso()
    n = 0
    for b in bars:
        conn.execute(
            """
            INSERT INTO daily_bars
                (ticker, session_date, open, high, low, close, volume, turnover,
                 source, ingested_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, session_date) DO UPDATE SET
                open     = COALESCE(excluded.open, daily_bars.open),
                high     = COALESCE(excluded.high, daily_bars.high),
                low      = COALESCE(excluded.low, daily_bars.low),
                close    = excluded.close,
                volume   = COALESCE(excluded.volume, daily_bars.volume),
                turnover = COALESCE(excluded.turnover, daily_bars.turnover),
                source   = excluded.source,
                ingested_utc = excluded.ingested_utc
            """,
            (
                b.ticker,
                b.session_date.isoformat(),
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                b.turnover,
                b.source,
                now,
            ),
        )
        n += 1
    conn.commit()
    return n


def upsert_index_levels(conn: sqlite3.Connection, levels: Iterable[IndexLevel]) -> int:
    now = utc_iso()
    n = 0
    for lvl in levels:
        conn.execute(
            """
            INSERT INTO index_levels
                (ticker, session_date, level, change_pct, source, ingested_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, session_date) DO UPDATE SET
                level = excluded.level,
                change_pct = excluded.change_pct,
                source = excluded.source,
                ingested_utc = excluded.ingested_utc
            """,
            (
                lvl.ticker,
                lvl.session_date.isoformat(),
                lvl.level,
                lvl.change_pct,
                lvl.source,
                now,
            ),
        )
        n += 1
    conn.commit()
    return n


def latest_snapshot_by_ticker(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return the newest snapshot per ticker (across sources), highest turnover first."""
    return list(
        conn.execute(
            """
            WITH latest AS (
                SELECT ticker, MAX(captured_utc) AS captured_utc
                FROM quote_snapshots
                GROUP BY ticker
            )
            SELECT qs.*
            FROM quote_snapshots qs
            JOIN latest l USING (ticker, captured_utc)
            ORDER BY COALESCE(qs.turnover, 0) DESC
            """
        ).fetchall()
    )


def open_fetch_log(conn: sqlite3.Connection, source: str, target: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO fetch_log (source, target, started_utc, status)
        VALUES (?, ?, ?, 'started')
        """,
        (source, target, utc_iso()),
    )
    conn.commit()
    return cur.lastrowid or 0


def close_fetch_log(
    conn: sqlite3.Connection,
    log_id: int,
    status: str,
    *,
    http_status: int | None = None,
    rows: int | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE fetch_log
        SET finished_utc = ?, status = ?, http_status = ?, rows_written = ?, error = ?
        WHERE id = ?
        """,
        (utc_iso(), status, http_status, rows, error, log_id),
    )
    conn.commit()
