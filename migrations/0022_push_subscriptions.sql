-- PR-AA: Web Push subscriptions — one row per (user, device/browser).
--
-- Push is per *device*, so this keys on the user, not the account: the
-- same person on a phone and a laptop is two rows, and an account with
-- two members fans out to every device either of them enabled. Alert
-- delivery joins account_members to find them (see services/alerts).
--
-- `endpoint` is the push service URL the browser handed us; it is unique
-- across the whole table because a device re-subscribing after a sign-in
-- as a different user must move to that user, not duplicate.
--
-- `p256dh` and `auth` are the client's ECDH public key and auth secret,
-- base64url as the browser gives them (RFC 8291). `last_error` keeps the
-- most recent failure note so /alerts can say why a device went quiet;
-- a 404/410 from the push service deletes the row outright.

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint       TEXT    NOT NULL UNIQUE,
    p256dh         TEXT    NOT NULL,
    auth           TEXT    NOT NULL,
    user_agent     TEXT    NOT NULL DEFAULT '',
    created_utc    TEXT    NOT NULL,
    last_used_utc  TEXT,
    last_error     TEXT
);

CREATE INDEX IF NOT EXISTS ix_push_subscriptions_user
    ON push_subscriptions(user_id);
