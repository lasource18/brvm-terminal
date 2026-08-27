-- Bond-specific columns on `securities` + bond_snapshots table (Phase 8b).
--
-- Phase 8 ingested bonds into `securities` with only the equity-shared
-- columns populated (ticker, name, kind, country, sector, source_url).
-- Phase 8b adds four reference fields parsed from the `Nom` cell and the
-- `Date émission` cell, plus a `bond_snapshots` table for the two daily
-- values the exchange publishes alongside the price (accrued coupon +
-- last-payment date/amount).
--
-- Rationale for splitting:
--   * Reference fields (coupon_rate / maturity_year / issue_date / issuer_name)
--     never change once a bond is admitted, so they sit on `securities`
--     next to the existing per-issuer facts (shares_outstanding et al.).
--   * Accrued coupon changes daily and the last-payment fields change
--     twice a year — those get their own snapshot table so `daily_bars`
--     stays clean of bond-only NULLs.

ALTER TABLE securities ADD COLUMN coupon_rate    REAL;
ALTER TABLE securities ADD COLUMN maturity_year  INTEGER;
ALTER TABLE securities ADD COLUMN issue_date     TEXT;
ALTER TABLE securities ADD COLUMN issuer_name    TEXT;

CREATE TABLE IF NOT EXISTS bond_snapshots (
    ticker              TEXT NOT NULL REFERENCES securities(ticker),
    session_date        TEXT NOT NULL,
    accrued_coupon      REAL,
    last_coupon_date    TEXT,
    last_coupon_amount  REAL,
    source              TEXT NOT NULL,
    ingested_utc        TEXT NOT NULL,
    PRIMARY KEY (ticker, session_date)
);

CREATE INDEX IF NOT EXISTS ix_securities_issuer_name ON securities(issuer_name);
