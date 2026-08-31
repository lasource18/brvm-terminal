"""SQLite repository for watchlists + watchlist_items."""

from __future__ import annotations

import re
import sqlite3

from kodji.clock import utc_iso

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return s or "list"


def create(conn: sqlite3.Connection, name: str, slug: str | None = None) -> int:
    slug = slug or slugify(name)
    now = utc_iso()
    cur = conn.execute(
        """
        INSERT INTO watchlists (slug, name, sort_order, created_utc)
        VALUES (?, ?, COALESCE((SELECT MAX(sort_order) + 1 FROM watchlists), 0), ?)
        """,
        (slug, name, now),
    )
    conn.commit()
    return cur.lastrowid or 0


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM watchlists ORDER BY sort_order, id"
        ).fetchall()
    )


def get_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM watchlists WHERE slug = ?", (slug,)
    ).fetchone()


def delete(conn: sqlite3.Connection, slug: str) -> int:
    cur = conn.execute("DELETE FROM watchlists WHERE slug = ?", (slug,))
    conn.commit()
    return cur.rowcount


def items(conn: sqlite3.Connection, watchlist_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT wi.ticker, wi.sort_order, wi.added_utc, s.name
            FROM watchlist_items wi
            JOIN securities s USING (ticker)
            WHERE wi.watchlist_id = ?
            ORDER BY wi.sort_order, wi.added_utc
            """,
            (watchlist_id,),
        ).fetchall()
    )


def add_item(conn: sqlite3.Connection, watchlist_id: int, ticker: str) -> bool:
    """Insert; return True if a new row was added, False if it already existed."""
    now = utc_iso()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO watchlist_items
            (watchlist_id, ticker, sort_order, added_utc)
        VALUES (
            ?, ?,
            COALESCE((SELECT MAX(sort_order) + 1 FROM watchlist_items WHERE watchlist_id = ?), 0),
            ?
        )
        """,
        (watchlist_id, ticker, watchlist_id, now),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_item(conn: sqlite3.Connection, watchlist_id: int, ticker: str) -> int:
    cur = conn.execute(
        "DELETE FROM watchlist_items WHERE watchlist_id = ? AND ticker = ?",
        (watchlist_id, ticker),
    )
    conn.commit()
    return cur.rowcount
