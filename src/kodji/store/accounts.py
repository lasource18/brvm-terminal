"""SQLite repository for accounts, users, membership and subscriptions.

Ownership in this codebase keys on `account_id`, never `user_id`. A
personal account is created per user at signup; a team account is the same
row with more members. Everything a customer owns — watchlists, alert
rules, a subscription — hangs off the account, so adding teams later is a
membership change rather than a migration across every scoped table.

`DEFAULT_ACCOUNT_ID` is the account migration 0017 assigns to all
pre-existing data. It is also what a request with no session resolves to
while `AUTH_REQUIRED` is off, and what local processes (TUI, jobs) always
resolve to — see `services/accounts.py` for that decision.
"""

from __future__ import annotations

import sqlite3

from kodji.clock import utc_iso

DEFAULT_ACCOUNT_ID = 1

FREE_PLAN = "free"
PAID_PLAN = "paid"
_ACTIVE_STATUSES = ("active", "past_due")


# --- accounts --------------------------------------------------------------


def create_account(
    conn: sqlite3.Connection, name: str, kind: str = "personal"
) -> int:
    cur = conn.execute(
        "INSERT INTO accounts (name, kind, created_utc) VALUES (?, ?, ?)",
        (name, kind, utc_iso()),
    )
    account_id = int(cur.lastrowid or 0)
    # Every account starts on the free plan, so plan lookups never have to
    # special-case a missing subscription row.
    now = utc_iso()
    conn.execute(
        """
        INSERT INTO subscriptions (account_id, plan, status, created_utc, updated_utc)
        VALUES (?, ?, 'active', ?, ?)
        """,
        (account_id, FREE_PLAN, now, now),
    )
    conn.commit()
    return account_id


def get_account(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()


# --- users + membership ----------------------------------------------------


def create_user(
    conn: sqlite3.Connection,
    email: str,
    locale: str = "fr",
    tz: str = "Africa/Abidjan",
) -> int:
    cur = conn.execute(
        "INSERT INTO users (email, created_utc, locale, tz) VALUES (?, ?, ?, ?)",
        (email.strip(), utc_iso(), locale, tz),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def get_user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    # Matches the lower(email) unique index from migration 0017.
    return conn.execute(
        "SELECT * FROM users WHERE lower(email) = lower(?)", (email.strip(),)
    ).fetchone()


def add_member(
    conn: sqlite3.Connection, account_id: int, user_id: int, role: str = "owner"
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO account_members (account_id, user_id, role, created_utc)
        VALUES (?, ?, ?, ?)
        """,
        (account_id, user_id, role, utc_iso()),
    )
    conn.commit()


def accounts_for_user(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT a.*, m.role
            FROM accounts a
            JOIN account_members m ON m.account_id = a.id
            WHERE m.user_id = ?
            ORDER BY a.id
            """,
            (user_id,),
        ).fetchall()
    )


def create_user_with_personal_account(
    conn: sqlite3.Connection, email: str
) -> tuple[int, int]:
    """Signup: one user, one personal account, one owner membership.

    Returns `(user_id, account_id)`.
    """
    user_id = create_user(conn, email)
    account_id = create_account(conn, name=email.strip(), kind="personal")
    add_member(conn, account_id, user_id, role="owner")
    return user_id, account_id


def ensure_user_with_account(
    conn: sqlite3.Connection, email: str
) -> tuple[int, int]:
    """Get-or-create the (user, account) pair for an email address.

    Idempotent on email, because magic-link sign-in has no separate
    "register" step: the same submitted address must mean "sign me in"
    for a returning user and "make me an account" for a new one. Running
    it twice returns the same pair rather than minting a second personal
    account.

    The middle branch covers a user whose memberships were all removed —
    a team-management bug or a manual DB edit. Re-homing them onto a
    fresh personal account is better than handing back a user id with no
    account, which every scoped query downstream would then reject.
    """
    existing = get_user_by_email(conn, email)
    if existing is None:
        return create_user_with_personal_account(conn, email)

    user_id = int(existing["id"])
    accounts = accounts_for_user(conn, user_id)
    if accounts:
        return user_id, int(accounts[0]["id"])

    account_id = create_account(conn, name=email.strip(), kind="personal")
    add_member(conn, account_id, user_id, role="owner")
    return user_id, account_id


def attach_user_to_account(
    conn: sqlite3.Connection, email: str, account_id: int, role: str = "owner"
) -> tuple[int, bool]:
    """Make `email` a member of an existing account, creating the user
    row if needed. Returns `(user_id, membership_created)`.

    Exists for one job: handing the operator their own data. Migration
    0017 seeded account 1 with everything the database held before
    multi-tenancy, and 0019 put it on the paid plan — but no user row
    points at it, so the operator's first magic-link sign-in would mint
    a fresh free account instead (`ensure_user_with_account`). Running
    this before `AUTH_REQUIRED=true` closes that gap. Idempotent, and
    account 1 sorts first in `accounts_for_user`, so it also wins over a
    personal account the operator created by signing in too early.
    """
    existing = get_user_by_email(conn, email)
    user_id = int(existing["id"]) if existing is not None else create_user(conn, email)
    already = any(int(a["id"]) == account_id for a in accounts_for_user(conn, user_id))
    if already:
        return user_id, False
    add_member(conn, account_id, user_id, role=role)
    return user_id, True


# --- subscriptions ---------------------------------------------------------


def get_subscription(
    conn: sqlite3.Connection, account_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM subscriptions WHERE account_id = ?", (account_id,)
    ).fetchone()


def plan_for(conn: sqlite3.Connection, account_id: int) -> str:
    """The plan gating should enforce for this account.

    A subscription that is cancelled, expired, or missing reads as free —
    the safe direction. `past_due` still counts as paid so a failed mobile
    money retry doesn't lock a paying customer out mid-cycle.
    """
    row = get_subscription(conn, account_id)
    if row is None:
        return FREE_PLAN
    if row["status"] not in _ACTIVE_STATUSES:
        return FREE_PLAN
    # PR-Z: a paid period that has ended is free the moment it ends, not
    # when the hourly expiry job gets round to stamping it. NULL means no
    # end (the operator's account from 0019).
    end = row["current_period_end_utc"]
    if end and end < utc_iso():
        return FREE_PLAN
    return str(row["plan"] or FREE_PLAN)


def set_plan(
    conn: sqlite3.Connection,
    account_id: int,
    plan: str,
    *,
    provider: str | None = None,
    provider_ref: str | None = None,
    status: str = "active",
    current_period_end_utc: str | None = None,
) -> None:
    now = utc_iso()
    conn.execute(
        """
        INSERT INTO subscriptions
            (account_id, plan, provider, provider_ref, status,
             current_period_end_utc, created_utc, updated_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            plan = excluded.plan,
            provider = excluded.provider,
            provider_ref = excluded.provider_ref,
            status = excluded.status,
            current_period_end_utc = excluded.current_period_end_utc,
            updated_utc = excluded.updated_utc
        """,
        (account_id, plan, provider, provider_ref, status,
         current_period_end_utc, now, now),
    )
    conn.commit()


def member_emails(conn: sqlite3.Connection, account_id: int) -> list[str]:
    """Every member's address — who billing mail goes to."""
    return [
        str(r["email"])
        for r in conn.execute(
            """
            SELECT u.email FROM users u
            JOIN account_members m ON m.user_id = u.id
            WHERE m.account_id = ?
            ORDER BY m.created_utc, u.id
            """,
            (account_id,),
        ).fetchall()
    ]


def lapsed_paid_subscriptions(conn: sqlite3.Connection, now_utc: str) -> list[sqlite3.Row]:
    """Paid, still marked active, but the period ended before `now_utc`."""
    return list(
        conn.execute(
            """
            SELECT * FROM subscriptions
            WHERE plan = ? AND status IN ('active', 'past_due')
              AND current_period_end_utc IS NOT NULL
              AND current_period_end_utc < ?
            ORDER BY current_period_end_utc
            """,
            (PAID_PLAN, now_utc),
        ).fetchall()
    )


def paid_subscriptions_ending_before(
    conn: sqlite3.Connection, horizon_utc: str, now_utc: str
) -> list[sqlite3.Row]:
    """Paid and active, ending between now and `horizon_utc` — the
    reminder window."""
    return list(
        conn.execute(
            """
            SELECT * FROM subscriptions
            WHERE plan = ? AND status = 'active'
              AND current_period_end_utc IS NOT NULL
              AND current_period_end_utc >= ? AND current_period_end_utc <= ?
            ORDER BY current_period_end_utc
            """,
            (PAID_PLAN, now_utc, horizon_utc),
        ).fetchall()
    )


def mark_expired(conn: sqlite3.Connection, account_id: int) -> int:
    """Bookkeeping for a lapsed period. The plan column is left at 'paid'
    so the history reads correctly; `plan_for` already returns free."""
    cur = conn.execute(
        "UPDATE subscriptions SET status = 'expired', updated_utc = ? WHERE account_id = ?",
        (utc_iso(), account_id),
    )
    conn.commit()
    return cur.rowcount
