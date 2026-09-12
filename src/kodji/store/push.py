"""SQLite repository for `push_subscriptions` (PR-AA).

A row is one device or browser that enabled notifications. The unique
key is the push-service `endpoint`; a device that subscribes again after
signing in as another user moves to that user rather than duplicating.
"""

from __future__ import annotations

import sqlite3

from kodji.clock import utc_iso
from kodji.models import PushSubscription


def _row(r: sqlite3.Row) -> PushSubscription:
    return PushSubscription(
        id=r["id"],
        user_id=r["user_id"],
        endpoint=r["endpoint"],
        p256dh=r["p256dh"],
        auth=r["auth"],
        user_agent=r["user_agent"] or "",
        created_utc=r["created_utc"],
        last_used_utc=r["last_used_utc"],
        last_error=r["last_error"],
    )


def upsert(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str = "",
) -> int:
    """Insert or refresh a device. Returns the row id.

    Re-subscribing rotates the client keys on some browsers, so the keys
    are always taken from the latest call. A refresh also clears
    `last_error`: the device is evidently alive again.
    """
    conn.execute(
        """
        INSERT INTO push_subscriptions
            (user_id, endpoint, p256dh, auth, user_agent, created_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET
            user_id = excluded.user_id,
            p256dh = excluded.p256dh,
            auth = excluded.auth,
            user_agent = excluded.user_agent,
            last_error = NULL
        """,
        (user_id, endpoint, p256dh, auth, user_agent[:200], utc_iso()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
    ).fetchone()
    return int(row["id"])


def delete_endpoint(conn: sqlite3.Connection, user_id: int, endpoint: str) -> int:
    """Remove one device. Scoped to the user so a request can only drop
    its own subscriptions, endpoints being unguessable notwithstanding."""
    cur = conn.execute(
        "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
        (user_id, endpoint),
    )
    conn.commit()
    return cur.rowcount


def delete_by_id(conn: sqlite3.Connection, sub_id: int, *, commit: bool = True) -> int:
    cur = conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))
    if commit:
        conn.commit()
    return cur.rowcount


def list_for_user(conn: sqlite3.Connection, user_id: int) -> list[PushSubscription]:
    rows = conn.execute(
        "SELECT * FROM push_subscriptions WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def count_for_user(conn: sqlite3.Connection, user_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
    )


def list_for_account(conn: sqlite3.Connection, account_id: int) -> list[PushSubscription]:
    """Every device of every member — the fan-out set for one account's
    alert rules."""
    rows = conn.execute(
        """
        SELECT p.* FROM push_subscriptions p
        JOIN account_members m ON m.user_id = p.user_id
        WHERE m.account_id = ?
        ORDER BY p.user_id, p.id
        """,
        (account_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def mark_result(
    conn: sqlite3.Connection,
    sub_id: int,
    *,
    ok: bool,
    note: str = "",
    commit: bool = True,
) -> None:
    """Stamp the outcome of one send so /alerts can show a device's health."""
    if ok:
        conn.execute(
            "UPDATE push_subscriptions SET last_used_utc = ?, last_error = NULL WHERE id = ?",
            (utc_iso(), sub_id),
        )
    else:
        conn.execute(
            "UPDATE push_subscriptions SET last_error = ? WHERE id = ?",
            (note[:200], sub_id),
        )
    if commit:
        conn.commit()
