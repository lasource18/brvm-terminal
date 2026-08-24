-- Alerts (Phase 6a).
--
-- alert_rules: user-configured triggers. `ticker` NULL matches every
-- security (watchlist-wide rules). `threshold_pct` reads as an absolute
-- value — the price-move evaluator fires on |change_pct| >= threshold,
-- so one rule covers both up-moves and down-moves. `doc_types` is a CSV
-- of FilingDocType keys (empty ⇒ any doc type).
--
-- alert_events: one row per triggered alert. `dedupe_key` is the natural
-- identity of the *thing* that fired (snapshot id / filing id / news id
-- + a coarse time bucket for price moves) so the store can reject
-- re-fires from a rule that keeps matching. `delivered_utc IS NULL` is
-- the queue for the delivery worker; the partial index makes the queue
-- scan free even after months of history.

CREATE TABLE IF NOT EXISTS alert_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,          -- 'price_move' | 'new_filing' | 'news'
    ticker          TEXT REFERENCES securities(ticker),
    threshold_pct   REAL,                   -- price_move: |change| trigger
    min_relevance   INTEGER,                -- news: Haiku relevance floor
    doc_types       TEXT,                   -- new_filing: CSV of FilingDocType
    label           TEXT,                   -- optional human name
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_utc     TEXT NOT NULL,
    CHECK (kind IN ('price_move', 'new_filing', 'news')),
    CHECK (enabled IN (0, 1))
);

CREATE INDEX IF NOT EXISTS ix_alert_rules_enabled
    ON alert_rules(enabled) WHERE enabled = 1;
CREATE INDEX IF NOT EXISTS ix_alert_rules_ticker ON alert_rules(ticker);

CREATE TABLE IF NOT EXISTS alert_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id         INTEGER NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,          -- copied from the rule at fire time
    ticker          TEXT,                   -- resolved ticker (may differ from rule.ticker for watchlist-wide rules)
    subject         TEXT NOT NULL,          -- short one-line title
    body            TEXT NOT NULL,          -- longer message (webhook body)
    payload_json    TEXT,                   -- structured context for debugging
    dedupe_key      TEXT NOT NULL,          -- (rule_id, dedupe_key) is unique
    fired_utc       TEXT NOT NULL,
    delivered_utc   TEXT,                   -- NULL = still queued
    delivery_status TEXT,                   -- 'ok' | 'failed' | 'skipped'
    UNIQUE (rule_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS ix_alert_events_undelivered
    ON alert_events(fired_utc) WHERE delivered_utc IS NULL;
CREATE INDEX IF NOT EXISTS ix_alert_events_fired
    ON alert_events(fired_utc DESC);
CREATE INDEX IF NOT EXISTS ix_alert_events_ticker
    ON alert_events(ticker);
