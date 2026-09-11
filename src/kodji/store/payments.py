"""SQLite repository for provider payments and billing notices (PR-Z).

`payments` is one row per checkout attempt, keyed by the `tx_ref` we
minted. It moves `pending` → `successful` exactly once (or → `failed`),
and the successful row records the paid period so support can answer
"what did this account pay for, when, until when" without the provider
dashboard.

`billing_notices` remembers which lifecycle mail went out for which
period end, so the daily reminder job is idempotent.
"""

from __future__ import annotations

import sqlite3

from kodji.clock import utc_iso

PENDING = "pending"
SUCCESSFUL = "successful"
FAILED = "failed"


def create_pending(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    tx_ref: str,
    period: str,
    amount_xof: int,
    customer_email: str,
    provider: str = "flutterwave",
) -> int:
    now = utc_iso()
    cur = conn.execute(
        """
        INSERT INTO payments
            (account_id, tx_ref, period, amount_xof, currency, status, provider,
             customer_email, created_utc, updated_utc)
        VALUES (?, ?, ?, ?, 'XOF', 'pending', ?, ?, ?, ?)
        """,
        (account_id, tx_ref, period, amount_xof, provider, customer_email, now, now),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def get_by_tx_ref(conn: sqlite3.Connection, tx_ref: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM payments WHERE tx_ref = ?", (tx_ref,)).fetchone()


def mark_successful(
    conn: sqlite3.Connection,
    tx_ref: str,
    *,
    provider_tx_id: str,
    provider_ref: str,
    payment_type: str,
    paid_utc: str,
    period_start_utc: str,
    period_end_utc: str,
    raw_json: str = "",
) -> int:
    """Only a non-successful row is updated — the second confirmation of
    the same payment is a no-op at the SQL level too."""
    cur = conn.execute(
        """
        UPDATE payments SET
            status = 'successful', provider_tx_id = ?, provider_ref = ?,
            payment_type = ?, paid_utc = ?, period_start_utc = ?,
            period_end_utc = ?, raw_json = ?, note = '', updated_utc = ?
        WHERE tx_ref = ? AND status != 'successful'
        """,
        (
            provider_tx_id,
            provider_ref,
            payment_type,
            paid_utc,
            period_start_utc,
            period_end_utc,
            raw_json,
            utc_iso(),
            tx_ref,
        ),
    )
    conn.commit()
    return cur.rowcount


def mark_failed(conn: sqlite3.Connection, tx_ref: str, *, note: str = "") -> int:
    cur = conn.execute(
        """
        UPDATE payments SET status = 'failed', note = ?, updated_utc = ?
        WHERE tx_ref = ? AND status = 'pending'
        """,
        (note[:200], utc_iso(), tx_ref),
    )
    conn.commit()
    return cur.rowcount


def list_for_account(
    conn: sqlite3.Connection, account_id: int, *, limit: int = 50
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM payments WHERE account_id = ?
            ORDER BY created_utc DESC LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
    )


def recent(conn: sqlite3.Connection, *, limit: int = 50) -> list[sqlite3.Row]:
    """Every account's recent payments — for the operator, not a page."""
    return list(
        conn.execute(
            "SELECT * FROM payments ORDER BY created_utc DESC LIMIT ?", (limit,)
        ).fetchall()
    )


# --- notices ----------------------------------------------------------------


def notice_sent(conn: sqlite3.Connection, account_id: int, period_end_utc: str, kind: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM billing_notices
        WHERE account_id = ? AND period_end_utc = ? AND kind = ?
        """,
        (account_id, period_end_utc, kind),
    ).fetchone()
    return row is not None


def record_notice(
    conn: sqlite3.Connection, account_id: int, period_end_utc: str, kind: str, sent_utc: str
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO billing_notices (account_id, period_end_utc, kind, sent_utc)
        VALUES (?, ?, ?, ?)
        """,
        (account_id, period_end_utc, kind, sent_utc),
    )
    conn.commit()
