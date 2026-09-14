"""SQLite repository for `pageviews` and the daily visitor salt.

Every read here is an aggregate. There is no "show me one visitor's
history" function and there should not be: the point of the daily salt is
that such a history cannot be reconstructed after the day rolls.
"""

from __future__ import annotations

import sqlite3


def insert_view(
    conn: sqlite3.Connection,
    *,
    ts_utc: str,
    day: str,
    path: str,
    status: int,
    referrer_host: str | None,
    visitor_hash: str,
    locale: str | None,
    signed_in: bool,
    plan: str | None,
    is_pwa: bool,
) -> None:
    conn.execute(
        """
        INSERT INTO pageviews
            (ts_utc, day, path, status, referrer_host, visitor_hash,
             locale, signed_in, plan, is_pwa)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts_utc, day, path, status, referrer_host, visitor_hash,
         locale, int(signed_in), plan, int(is_pwa)),
    )
    conn.commit()


def get_salt(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """The current `(day, salt)`, or None on a fresh install."""
    row = conn.execute("SELECT day, salt FROM analytics_salt WHERE id = 1").fetchone()
    return (str(row["day"]), str(row["salt"])) if row else None


def put_salt(conn: sqlite3.Connection, day: str, salt: str) -> None:
    """Overwrite the salt in place.

    Replace, never append. A second row would keep a previous day's salt
    alive, and with it the ability to re-derive that day's hashes from an
    IP address — exactly what this design exists to prevent.
    """
    conn.execute(
        "INSERT INTO analytics_salt (id, day, salt) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET day = excluded.day, salt = excluded.salt",
        (day, salt),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


def daily_totals(conn: sqlite3.Connection, since_day: str) -> list[sqlite3.Row]:
    """Views and distinct visitors per day, newest first."""
    return conn.execute(
        """
        SELECT day,
               COUNT(*)                     AS views,
               COUNT(DISTINCT visitor_hash) AS visitors,
               SUM(signed_in)               AS signed_in_views,
               SUM(is_pwa)                  AS pwa_views
        FROM pageviews
        WHERE day >= ?
        GROUP BY day
        ORDER BY day DESC
        """,
        (since_day,),
    ).fetchall()


def top_paths(conn: sqlite3.Connection, since_day: str, limit: int = 15) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT path,
               COUNT(*)                     AS views,
               COUNT(DISTINCT visitor_hash) AS visitors
        FROM pageviews
        WHERE day >= ?
        GROUP BY path
        ORDER BY views DESC, path
        LIMIT ?
        """,
        (since_day, limit),
    ).fetchall()


def top_referrers(conn: sqlite3.Connection, since_day: str, limit: int = 15) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT referrer_host,
               COUNT(*)                     AS views,
               COUNT(DISTINCT visitor_hash) AS visitors
        FROM pageviews
        WHERE day >= ? AND referrer_host IS NOT NULL
        GROUP BY referrer_host
        ORDER BY views DESC, referrer_host
        LIMIT ?
        """,
        (since_day, limit),
    ).fetchall()


def locale_split(conn: sqlite3.Connection, since_day: str) -> list[sqlite3.Row]:
    """Which language visitors actually get — the check on whether the
    Accept-Language negotiation is doing what it was meant to."""
    return conn.execute(
        """
        SELECT locale,
               COUNT(*)                     AS views,
               COUNT(DISTINCT visitor_hash) AS visitors
        FROM pageviews
        WHERE day >= ?
        GROUP BY locale
        ORDER BY views DESC
        """,
        (since_day,),
    ).fetchall()


def visitors_on_path(conn: sqlite3.Connection, since_day: str, path: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(DISTINCT visitor_hash) FROM pageviews "
            "WHERE day >= ? AND path = ?",
            (since_day, path),
        ).fetchone()[0]
    )


def total_visitors(conn: sqlite3.Connection, since_day: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(DISTINCT visitor_hash) FROM pageviews WHERE day >= ?",
            (since_day,),
        ).fetchone()[0]
    )


def prune(conn: sqlite3.Connection, before_day: str) -> int:
    cur = conn.execute("DELETE FROM pageviews WHERE day < ?", (before_day,))
    conn.commit()
    return cur.rowcount
