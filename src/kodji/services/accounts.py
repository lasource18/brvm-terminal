"""Account resolution for the current request or session.

Everything a customer owns hangs off an `account_id`. This module is the
single place that decides *which* account a caller is acting on, so
authentication (PR-X2) changes one function body rather than every route.

Until sign-in exists there is exactly one account — the one migration 0017
assigned all pre-existing data to — and `current_account_id` returns it.
The scoping underneath is real either way: the repositories already
require an account and filter on it, so the day sessions arrive the only
change is where this number comes from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kodji.config import settings
from kodji.db import connect
from kodji.store import accounts as repo
from kodji.store.accounts import DEFAULT_ACCOUNT_ID, FREE_PLAN, PAID_PLAN

__all__ = [
    "DEFAULT_ACCOUNT_ID",
    "FREE_PLAN",
    "PAID_PLAN",
    "current_account_id",
    "plan_for",
    "signup",
]


def _db_path() -> Path:
    return Path(settings.db_path)


def current_account_id(request: Any = None) -> int:
    """The account the caller is acting on.

    PR-X2 replaces the body with a session-cookie lookup:
    read the signed token, hash it, `SELECT account_id FROM sessions`,
    fall back to raising an auth error. The signature already accepts the
    request so no call site has to change when that lands.
    """
    del request  # unused until sessions exist
    return DEFAULT_ACCOUNT_ID


def plan_for(account_id: int) -> str:
    """`"free"` or `"paid"` — what gating (PR-Y) enforces for this account."""
    with connect(_db_path()) as conn:
        return repo.plan_for(conn, account_id)


def signup(email: str) -> tuple[int, int]:
    """Create a user with their personal account. Returns (user_id, account_id).

    Idempotent on email: signing up twice returns the existing pair rather
    than creating a second personal account, which is what a magic-link
    flow needs — "sign in or sign up" is one action to the user.
    """
    with connect(_db_path()) as conn:
        existing = repo.get_user_by_email(conn, email)
        if existing is not None:
            accounts = repo.accounts_for_user(conn, int(existing["id"]))
            if accounts:
                return int(existing["id"]), int(accounts[0]["id"])
            account_id = repo.create_account(conn, name=email.strip())
            repo.add_member(conn, account_id, int(existing["id"]))
            return int(existing["id"]), account_id
        return repo.create_user_with_personal_account(conn, email)
