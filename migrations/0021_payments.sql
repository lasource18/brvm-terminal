-- PR-Z: Flutterwave billing — payments and lifecycle notices.
--
-- `payments` is one row per checkout attempt keyed by the tx_ref we mint.
-- It goes pending → successful once (or → failed). A successful row keeps
-- the paid period so support never needs the provider dashboard.
--
-- `billing_notices` records which lifecycle email went out for which
-- period end (expiring_7d, expiring_1d, expired) so the daily jobs are
-- idempotent.
--
-- `subscriptions` gains nothing: plan/status/current_period_end_utc from
-- 0017 already model a paid period. Expiry is `current_period_end_utc`
-- in the past; `plan_for` reads that as free and the hourly job stamps
-- status='expired' for the books.

CREATE TABLE IF NOT EXISTS payments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    tx_ref           TEXT    NOT NULL UNIQUE,
    period           TEXT    NOT NULL,                -- month | year
    amount_xof       INTEGER NOT NULL,                -- zero-decimal francs
    currency         TEXT    NOT NULL DEFAULT 'XOF',
    status           TEXT    NOT NULL DEFAULT 'pending',   -- pending | successful | failed
    provider         TEXT    NOT NULL DEFAULT 'flutterwave',
    provider_tx_id   TEXT,
    provider_ref     TEXT,
    payment_type     TEXT,
    customer_email   TEXT,
    note             TEXT    NOT NULL DEFAULT '',
    raw_json         TEXT,
    created_utc      TEXT    NOT NULL,
    updated_utc      TEXT    NOT NULL,
    paid_utc         TEXT,
    period_start_utc TEXT,
    period_end_utc   TEXT,
    CHECK (period IN ('month', 'year')),
    CHECK (status IN ('pending', 'successful', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_payments_account ON payments(account_id, created_utc);

CREATE TABLE IF NOT EXISTS billing_notices (
    account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    period_end_utc TEXT    NOT NULL,
    kind           TEXT    NOT NULL,                  -- expiring_7d | expiring_1d | expired
    sent_utc       TEXT    NOT NULL,
    PRIMARY KEY (account_id, period_end_utc, kind)
);
