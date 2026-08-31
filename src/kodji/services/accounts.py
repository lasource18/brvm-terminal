"""Account resolution for the current request or session.

Everything a customer owns hangs off an `account_id`. This module is the
single place that decides *which* account a caller is acting on — PR-X
built every repository to require one, and this is where the number comes
from.

Three callers, three answers:

* **A request with a valid session cookie** resolves to that session's
  account. This is the real path, and after PR-X2 it is the only one that
  runs for a signed-in user.
* **A request without one** resolves to the default account, unless
  `AUTH_REQUIRED` is set, in which case it raises `NotAuthenticated`. The
  flag is off today so the existing single-user deployment keeps working
  unchanged; PR-Y turns it on together with route-level plan gating.
  **Turn it on before the app is reachable by anyone but its owner** —
  with it off, an anonymous visitor reads account 1's data.
* **No request at all** — the TUI, a scheduled job, a CLI script — is a
  local process running as the machine's owner, so it gets the default
  account. There is no cookie to consult and nobody to redirect.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kodji.clock import utc_iso
from kodji.config import settings
from kodji.db import connect
from kodji.store import accounts as repo
from kodji.store import auth as auth_repo
from kodji.store.accounts import DEFAULT_ACCOUNT_ID, FREE_PLAN, PAID_PLAN
from kodji.store.auth import SESSION_COOKIE, hash_secret

__all__ = [
    "DEFAULT_ACCOUNT_ID",
    "FREE_PLAN",
    "PAID_PLAN",
    "Identity",
    "NotAuthenticated",
    "current_account_id",
    "identity_for",
    "plan_for",
    "signup",
]


class NotAuthenticated(Exception):
    """Raised when a request needs a session and hasn't got one.

    A plain exception rather than `HTTPException` on purpose: this is a
    service, and the TUI has no notion of a 303. The web layer installs a
    handler that turns it into a redirect to /login.
    """


@dataclass(frozen=True)
class Identity:
    """Who a request is acting as. Built from the session cookie."""

    user_id: int
    account_id: int
    email: str


def _db_path() -> Path:
    return Path(settings.db_path)


def identity_for(request: Any) -> Identity | None:
    """Resolve a request's session cookie to an identity, or None.

    Duck-typed on `.cookies` rather than typed against `starlette.Request`
    so this module stays free of a web framework — the TUI imports it too.

    The cookie value is hashed before it is used as a lookup key, so the
    raw token is never compared against anything stored, and an expired
    row is filtered out in SQL rather than here.
    """
    raw = getattr(request, "cookies", {}).get(SESSION_COOKIE) if request else None
    if not raw:
        return None
    with connect(_db_path()) as conn:
        row = auth_repo.get_active_session_with_user(conn, hash_secret(raw), utc_iso())
    if row is None:
        return None
    return Identity(
        user_id=int(row["user_id"]),
        account_id=int(row["account_id"]),
        email=str(row["email"]),
    )


def current_account_id(request: Any = None) -> int:
    """The account the caller is acting on.

    See the module docstring for the three cases. `request=None` means a
    local process (TUI, job, script), which is the machine owner and gets
    the default account — it never raises, so a scheduled job can't be
    broken by an auth setting.
    """
    if request is None:
        return DEFAULT_ACCOUNT_ID
    identity = identity_for(request)
    if identity is not None:
        return identity.account_id
    if settings.auth_required:
        raise NotAuthenticated
    return DEFAULT_ACCOUNT_ID


def plan_for(account_id: int) -> str:
    """`"free"` or `"paid"` — what gating (PR-Y) enforces for this account."""
    with connect(_db_path()) as conn:
        return repo.plan_for(conn, account_id)


def signup(email: str) -> tuple[int, int]:
    """Create a user with their personal account. Returns (user_id, account_id).

    Idempotent on email: signing up twice returns the existing pair rather
    than creating a second personal account, which is what a magic-link
    flow needs — "sign in or sign up" is one action to the user. The
    sign-in flow calls the repository directly so it can do this inside
    the transaction that consumes the challenge; this wrapper is for
    scripts and tests that just want an account.
    """
    with connect(_db_path()) as conn:
        return repo.ensure_user_with_account(conn, email)
