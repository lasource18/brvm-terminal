-- PR-Y: the pre-existing owner account is paid.
--
-- Migration 0017 seeded account 1 with plan 'free' because there was no
-- gating yet and 'free' was simply the column default. PR-Y turns that
-- default into a real restriction, which would lock this deployment's
-- owner out of their own charts, financials, brief and alerts on the
-- next migrate — the opposite of what gating is for.
--
-- Account 1 is the account that owned everything in the database before
-- multi-tenancy existed (see 0017's seed block). Gating exists for
-- accounts that sign up later; this one is the operator's.
--
-- Scoped to id = 1 deliberately: a fresh signup gets its own account and
-- its own 'free' subscription from `ensure_user_with_account`, and this
-- statement must never touch those.

UPDATE subscriptions
SET plan = 'paid',
    status = 'active',
    updated_utc = datetime('now')
WHERE account_id = 1;
