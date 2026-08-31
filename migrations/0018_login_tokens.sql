-- PR-X2: magic-link sign-in.
--
-- `sessions` already exists (migration 0017). What was missing is the
-- short-lived challenge that mints one: a row per sign-in request,
-- carrying both a URL token and a typed code for the same challenge.
--
-- Why both, when either alone would work:
--   * the LINK is the fast path — one tap from the mail client;
--   * the CODE rescues the case the link cannot serve — a mail app that
--     opens the message in an in-app browser with no cookie jar, or a
--     user on a phone reading mail on a different device than the one
--     with the session. On mobile-first West African traffic that is not
--     an edge case, so the code is a first-class path, not a fallback.
--
-- Both are stored as SHA-256 digests for the same reason as `sessions`:
-- a leaked database must not hand over live sign-ins. The raw values only
-- ever exist in the email that was sent.

CREATE TABLE IF NOT EXISTS login_tokens (
    token_hash    TEXT PRIMARY KEY,
    code_hash     TEXT NOT NULL,
    email         TEXT NOT NULL,
    -- Locale of the request that asked for the link, so a second device
    -- opening the mail gets the language the user was actually browsing
    -- in rather than the account default.
    locale        TEXT NOT NULL DEFAULT 'fr',
    created_utc   TEXT NOT NULL,
    expires_utc   TEXT NOT NULL,
    consumed_utc  TEXT,
    -- Wrong-code guesses against this challenge. The 6-digit code has a
    -- 1-in-a-million space per try, which is only safe with a cap.
    attempts      INTEGER NOT NULL DEFAULT 0
);

-- Serves both the code path (newest live challenge for an address) and
-- the per-address rate limit (how many were minted in the last hour).
CREATE INDEX IF NOT EXISTS ix_login_tokens_email
    ON login_tokens(lower(email), created_utc);
CREATE INDEX IF NOT EXISTS ix_login_tokens_expires
    ON login_tokens(expires_utc);
