-- PR-X: accounts, users, sessions, subscriptions + account scoping.
--
-- The app was built single-user: `watchlists` and `alert_rules` carry no
-- owner at all, and `watchlists.slug` is globally UNIQUE — so the second
-- user to create a list called "Banks" collides with the first.
--
-- Ownership keys on `account_id`, not `user_id`. A personal account is
-- auto-created per user at signup, and a team account is the same shape
-- with more members. Retrofitting that distinction after there are paying
-- customers would be a migration across every scoped table, so it lands
-- now while there is exactly one account to migrate.
--
-- `watchlists` and `alert_rules` are rebuilt rather than ALTERed: the
-- column-level UNIQUE on `slug` creates an implicit index that SQLite
-- cannot drop in place, and ADD COLUMN cannot introduce a NOT NULL
-- REFERENCES column while foreign keys are enabled. The 12-step rebuild
-- below is the documented pattern; `watchlist_items` and `alert_events`
-- keep pointing at the rebuilt parents because the replacement table
-- takes the original name.

PRAGMA foreign_keys=OFF;

-- --- identity ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'personal',
    created_utc  TEXT NOT NULL,
    CHECK (kind IN ('personal', 'team'))
);

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT NOT NULL,
    created_utc  TEXT NOT NULL,
    locale       TEXT NOT NULL DEFAULT 'fr',
    tz           TEXT NOT NULL DEFAULT 'Africa/Abidjan'
);
-- Case-insensitive: nobody should be able to register Foo@x and foo@x.
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON users(lower(email));

CREATE TABLE IF NOT EXISTS account_members (
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role         TEXT NOT NULL DEFAULT 'owner',
    created_utc  TEXT NOT NULL,
    PRIMARY KEY (account_id, user_id),
    CHECK (role IN ('owner', 'member'))
);
CREATE INDEX IF NOT EXISTS ix_account_members_user ON account_members(user_id);

-- Session tokens are stored hashed: a leaked DB must not hand over live
-- sessions. The raw token only ever exists in the user's cookie.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_utc  TEXT NOT NULL,
    expires_utc  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_expires ON sessions(expires_utc);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);

-- One current subscription per account. `provider`/`provider_ref` stay
-- generic so a second processor is an adapter, not a schema change.
CREATE TABLE IF NOT EXISTS subscriptions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id              INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    plan                    TEXT NOT NULL DEFAULT 'free',
    provider                TEXT,
    provider_ref            TEXT,
    status                  TEXT NOT NULL DEFAULT 'active',
    current_period_end_utc  TEXT,
    created_utc             TEXT NOT NULL,
    updated_utc             TEXT NOT NULL,
    CHECK (plan IN ('free', 'paid')),
    CHECK (status IN ('active', 'past_due', 'canceled', 'expired'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_subscriptions_account
    ON subscriptions(account_id);
-- Makes provider webhooks idempotent: the same provider_ref cannot land twice.
CREATE UNIQUE INDEX IF NOT EXISTS ux_subscriptions_provider_ref
    ON subscriptions(provider, provider_ref) WHERE provider_ref IS NOT NULL;

-- --- the account that owns everything already in this database --------
--
-- Deliberately no `users` row: a user arrives with authentication (PR-X2),
-- and inventing an email address here would create a login that nobody
-- controls. The account can own data without a member.

INSERT INTO accounts (id, name, kind, created_utc)
SELECT 1, 'Personal', 'personal', datetime('now')
WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE id = 1);

INSERT INTO subscriptions (account_id, plan, status, created_utc, updated_utc)
SELECT 1, 'free', 'active', datetime('now'), datetime('now')
WHERE NOT EXISTS (SELECT 1 FROM subscriptions WHERE account_id = 1);

-- --- watchlists: add account_id, drop the global slug UNIQUE ----------

CREATE TABLE watchlists_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    slug         TEXT NOT NULL,
    name         TEXT NOT NULL,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_utc  TEXT NOT NULL
);
INSERT INTO watchlists_new (id, account_id, slug, name, sort_order, created_utc)
    SELECT id, 1, slug, name, sort_order, created_utc FROM watchlists;
DROP TABLE watchlists;
ALTER TABLE watchlists_new RENAME TO watchlists;

-- The constraint this migration exists to fix: unique per account, not globally.
CREATE UNIQUE INDEX ux_watchlists_account_slug ON watchlists(account_id, slug);
CREATE INDEX ix_watchlists_account ON watchlists(account_id);

-- --- alert_rules: add account_id --------------------------------------

CREATE TABLE alert_rules_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    ticker          TEXT REFERENCES securities(ticker),
    threshold_pct   REAL,
    min_relevance   INTEGER,
    doc_types       TEXT,
    label           TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_utc     TEXT NOT NULL,
    CHECK (kind IN ('price_move', 'new_filing', 'news')),
    CHECK (enabled IN (0, 1))
);
INSERT INTO alert_rules_new
    (id, account_id, kind, ticker, threshold_pct, min_relevance,
     doc_types, label, enabled, created_utc)
    SELECT id, 1, kind, ticker, threshold_pct, min_relevance,
           doc_types, label, enabled, created_utc
    FROM alert_rules;
DROP TABLE alert_rules;
ALTER TABLE alert_rules_new RENAME TO alert_rules;

CREATE INDEX ix_alert_rules_account ON alert_rules(account_id);
-- Preserves the evaluator's hot path from 0009.
CREATE INDEX ix_alert_rules_enabled ON alert_rules(enabled);

PRAGMA foreign_keys=ON;
