"""Make an email address the owner of the operator account (id 1).

Why this exists: migration 0017 seeded account 1 with everything the
database held before multi-tenancy — your watchlists, alert rules, notes —
and 0019 put it on the paid plan. But no user row points at it, so with
`AUTH_REQUIRED=true` your first magic-link sign-in would create a fresh
free account and your own data would be invisible. Run this once per
deployment, before you turn `AUTH_REQUIRED` on:

    uv run python scripts/claim_owner.py you@example.com
    # or: just claim-owner you@example.com

Idempotent. A session minted before the claim keeps its old account id —
sign out and back in afterwards.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kodji.config import settings  # noqa: E402
from kodji.db import assert_schema_current, connect  # noqa: E402
from kodji.services.auth import normalize_email  # noqa: E402
from kodji.store import accounts as accounts_repo  # noqa: E402
from kodji.store.accounts import DEFAULT_ACCOUNT_ID  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: claim_owner.py <email>", file=sys.stderr)
        return 2
    email = normalize_email(argv[0])
    if email is None:
        print(f"[claim-owner] not an email address: {argv[0]!r}", file=sys.stderr)
        return 2

    # Same guard as the app: a half-migrated DB has no account 1 to claim.
    assert_schema_current(settings.db_path)

    with connect(settings.db_path) as conn:
        if accounts_repo.get_account(conn, DEFAULT_ACCOUNT_ID) is None:
            print(f"[claim-owner] account {DEFAULT_ACCOUNT_ID} does not exist", file=sys.stderr)
            return 1
        user_id, created = accounts_repo.attach_user_to_account(conn, email, DEFAULT_ACCOUNT_ID)
        plan = accounts_repo.plan_for(conn, DEFAULT_ACCOUNT_ID)

    verb = "now owns" if created else "already owns"
    print(f"[claim-owner] {email} (user {user_id}) {verb} account {DEFAULT_ACCOUNT_ID} [{plan}]")
    if created:
        print("[claim-owner] sessions opened before this keep their old account: sign out and in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
