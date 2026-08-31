"""SQLite repository for watchlists + watchlist_items.

Every function that touches a watchlist takes `account_id` as a required
positional argument. That is deliberate: a default would make it possible
to add a call site that quietly reads across tenants, and the failure mode
of a missed scope here is one customer seeing another's data. Required
means a missed call site is a TypeError at import/collection time, which
the suite catches, instead of a silent leak in production.
"""

from __future__ import annotations

import re
import sqlite3

from kodji.clock import utc_iso

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return s or "list"


def create(
    conn: sqlite3.Connection, account_id: int, name: str, slug: str | None = None
) -> int:
    slug = slug or slugify(name)
    now = utc_iso()
    cur = conn.execute(
        """
        INSERT INTO watchlists (account_id, slug, name, sort_order, created_utc)
        VALUES (
            ?, ?, ?,
            COALESCE((SELECT MAX(sort_order) + 1 FROM watchlists WHERE account_id = ?), 0),
            ?
        )
        """,
        (account_id, slug, name, account_id, now),
    )
    conn.commit()
    return cur.lastrowid or 0


def list_all(conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM watchlists WHERE account_id = ? ORDER BY sort_order, id",
            (account_id,),
        ).fetchall()
    )


def get_by_slug(
    conn: sqlite3.Connection, account_id: int, slug: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM watchlists WHERE account_id = ? AND slug = ?",
        (account_id, slug),
    ).fetchone()


def delete(conn: sqlite3.Connection, account_id: int, slug: str) -> int:
    cur = conn.execute(
        "DELETE FROM watchlists WHERE account_id = ? AND slug = ?",
        (account_id, slug),
    )
    conn.commit()
    return cur.rowcount


def count(conn: sqlite3.Connection, account_id: int) -> int:
    """Watchlists owned by an account."""
    return int(
        conn.execute(
            "SELECT count(*) FROM watchlists WHERE account_id = ?", (account_id,)
        ).fetchone()[0]
    )


def distinct_ticker_count(conn: sqlite3.Connection, account_id: int) -> int:
    """Distinct securities across all of an account's watchlists.

    This is what the free-plan cap is measured against (PR-Y) — a member
    watched on three lists is one security, not three.
    """
    return int(
        conn.execute(
            """
            SELECT count(DISTINCT wi.ticker)
            FROM watchlist_items wi
            JOIN watchlists w ON w.id = wi.watchlist_id
            WHERE w.account_id = ?
            """,
            (account_id,),
        ).fetchone()[0]
    )


# --- items -----------------------------------------------------------------
#
# Items are addressed by `watchlist_id`, which the caller must already have
# obtained through an account-scoped lookup above. They still re-check the
# account on every write so a forged id from a route parameter cannot reach
# another tenant's list.


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


def add_item(
    conn: sqlite3.Connection, account_id: int, watchlist_id: int, ticker: str
) -> bool:
    """Insert; return True if a new row was added, False if it already existed
    or the watchlist does not belong to `account_id`."""
    now = utc_iso()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO watchlist_items
            (watchlist_id, ticker, sort_order, added_utc)
        SELECT
            ?, ?,
            COALESCE((SELECT MAX(sort_order) + 1 FROM watchlist_items WHERE watchlist_id = ?), 0),
            ?
        WHERE EXISTS (SELECT 1 FROM watchlists WHERE id = ? AND account_id = ?)
        """,
        (watchlist_id, ticker, watchlist_id, now, watchlist_id, account_id),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_item(
    conn: sqlite3.Connection, account_id: int, watchlist_id: int, ticker: str
) -> int:
    cur = conn.execute(
        """
        DELETE FROM watchlist_items
        WHERE watchlist_id = ? AND ticker = ?
          AND EXISTS (SELECT 1 FROM watchlists WHERE id = ? AND account_id = ?)
        """,
        (watchlist_id, ticker, watchlist_id, account_id),
    )
    conn.commit()
    return cur.rowcount
