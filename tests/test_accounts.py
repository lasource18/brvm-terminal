"""PR-X: account ownership and tenant isolation.

The migration exists because `watchlists.slug` was globally UNIQUE and
neither watchlists nor alert rules carried an owner. These tests pin the
two things that had to become true: two accounts can hold the same slug,
and neither can reach the other's rows through any repository call.

Isolation is asserted at the repository layer on purpose. That is where a
missed `WHERE account_id` would actually leak, and it stays meaningful
after PR-X2 swaps `current_account_id` for a real session lookup.
"""

from __future__ import annotations

import pytest

from kodji.config import settings
from kodji.db import connect
from kodji.models import AlertRule
from kodji.store import accounts as accounts_repo
from kodji.store import alerts as alerts_repo
from kodji.store import watchlists as wl_repo
from kodji.store.accounts import DEFAULT_ACCOUNT_ID


@pytest.fixture()
def two_accounts(client):
    """`client` seeds and migrates the DB. Account 1 ships with the
    migration; account 2 stands in for a second customer."""
    with connect(settings.db_path) as conn:
        other = accounts_repo.create_account(conn, "Second customer")
    return DEFAULT_ACCOUNT_ID, other


def test_migration_seeds_the_default_account(client):
    with connect(settings.db_path) as conn:
        row = accounts_repo.get_account(conn, DEFAULT_ACCOUNT_ID)
        assert row is not None
        assert row["kind"] == "personal"
        # Deliberately memberless: a user arrives with authentication, and
        # inventing an email here would create a login nobody controls.
        members = conn.execute(
            "SELECT count(*) FROM account_members WHERE account_id = ?",
            (DEFAULT_ACCOUNT_ID,),
        ).fetchone()[0]
        assert members == 0


def test_same_slug_on_two_accounts(two_accounts):
    """The bug this migration exists for."""
    a, b = two_accounts
    with connect(settings.db_path) as conn:
        wl_repo.create(conn, a, "Banks")
        wl_repo.create(conn, b, "Banks")  # must not raise
        assert wl_repo.get_by_slug(conn, a, "banks") is not None
        assert wl_repo.get_by_slug(conn, b, "banks") is not None


def test_duplicate_slug_within_one_account_still_rejected(two_accounts):
    import sqlite3

    a, _ = two_accounts
    with connect(settings.db_path) as conn:
        wl_repo.create(conn, a, "Banks")
        with pytest.raises(sqlite3.IntegrityError):
            wl_repo.create(conn, a, "Banks")


def test_watchlists_are_not_visible_across_accounts(two_accounts):
    a, b = two_accounts
    with connect(settings.db_path) as conn:
        wl_repo.create(conn, a, "Mine")
        a_slugs = {r["slug"] for r in wl_repo.list_all(conn, a)}
        b_slugs = {r["slug"] for r in wl_repo.list_all(conn, b)}
        assert "mine" in a_slugs
        assert "mine" not in b_slugs
        assert wl_repo.get_by_slug(conn, b, "mine") is None


def test_another_account_cannot_delete_or_write_your_watchlist(two_accounts):
    a, b = two_accounts
    with connect(settings.db_path) as conn:
        wl_id = wl_repo.create(conn, a, "Mine")

        # A forged id in a URL must not reach across the tenant boundary.
        assert wl_repo.delete(conn, b, "mine") == 0
        assert wl_repo.add_item(conn, b, wl_id, "SNTS") is False
        assert wl_repo.remove_item(conn, b, wl_id, "SNTS") == 0

        # The owner still works normally.
        assert wl_repo.add_item(conn, a, wl_id, "SNTS") is True
        assert wl_repo.get_by_slug(conn, a, "mine") is not None


def test_alert_rules_are_not_visible_across_accounts(two_accounts):
    a, b = two_accounts
    rule = AlertRule(kind="price_move", ticker="SNTS", threshold_pct=3.0)
    with connect(settings.db_path) as conn:
        rid = alerts_repo.create_rule(conn, a, rule)

        assert [r.id for r in alerts_repo.list_rules(conn, a)] == [rid]
        assert alerts_repo.list_rules(conn, b) == []
        assert alerts_repo.get_rule(conn, b, rid) is None
        assert alerts_repo.set_enabled(conn, b, rid, False) == 0
        assert alerts_repo.delete_rule(conn, b, rid) == 0

        # Still there, still enabled, after all of that.
        owned = alerts_repo.get_rule(conn, a, rid)
        assert owned is not None and owned.enabled is True


def test_evaluator_reads_every_account(two_accounts):
    """The one legitimate cross-tenant read: the scheduler must evaluate
    every customer's rules in a single pass."""
    a, b = two_accounts
    with connect(settings.db_path) as conn:
        alerts_repo.create_rule(conn, a, AlertRule(kind="price_move", threshold_pct=1.0))
        alerts_repo.create_rule(conn, b, AlertRule(kind="price_move", threshold_pct=2.0))
        assert len(alerts_repo.list_all_enabled_rules(conn)) == 2
        # ...while the per-account view still sees only its own.
        assert len(alerts_repo.list_rules(conn, a)) == 1


def test_deleting_an_account_takes_its_data(two_accounts):
    a, b = two_accounts
    with connect(settings.db_path) as conn:
        wl_repo.create(conn, b, "Theirs")
        alerts_repo.create_rule(conn, b, AlertRule(kind="price_move", threshold_pct=1.0))
        conn.execute("DELETE FROM accounts WHERE id = ?", (b,))
        conn.commit()

        assert wl_repo.list_all(conn, b) == []
        assert alerts_repo.list_rules(conn, b) == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # The other account is untouched.
        assert accounts_repo.get_account(conn, a) is not None


def test_new_accounts_start_on_the_free_plan(two_accounts):
    a, b = two_accounts
    with connect(settings.db_path) as conn:
        # Account 1 is the operator's own — migration 0019 puts it on paid
        # so PR-Y's gating can't lock the owner out of their own terminal.
        assert accounts_repo.plan_for(conn, a) == "paid"
        # Anyone who signs up afterwards starts free.
        assert accounts_repo.plan_for(conn, b) == "free"

        accounts_repo.set_plan(conn, b, "paid", provider="flutterwave",
                               provider_ref="sub_123")
        assert accounts_repo.plan_for(conn, b) == "paid"

        c = accounts_repo.create_account(conn, "Third customer")
        # One account upgrading must not upgrade another.
        assert accounts_repo.plan_for(conn, c) == "free"


@pytest.mark.parametrize(
    "status,expected",
    [("active", "paid"), ("past_due", "paid"), ("canceled", "free"),
     ("expired", "free")],
)
def test_plan_reads_free_when_the_subscription_is_not_live(
    two_accounts, status, expected
):
    """`past_due` still counts as paid: a failed mobile-money retry must
    not lock a paying customer out mid-cycle. Everything else fails closed."""
    _, b = two_accounts
    with connect(settings.db_path) as conn:
        accounts_repo.set_plan(conn, b, "paid", provider="flutterwave",
                               provider_ref="sub_x", status=status)
        assert accounts_repo.plan_for(conn, b) == expected


def test_signup_is_idempotent_on_email(client):
    from kodji.services import accounts as accounts_svc

    first = accounts_svc.signup("Trader@Example.CI")
    again = accounts_svc.signup("trader@example.ci")  # different case
    assert first == again, "a second signup must not create a second account"

    with connect(settings.db_path) as conn:
        n = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        assert n == 1
