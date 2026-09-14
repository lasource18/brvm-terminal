-- Privacy-respecting, first-party pageview counting.
--
-- No third-party script, no analytics cookie, and no IP address or user
-- agent is ever stored. A visitor is counted via `visitor_hash`:
--
--     sha256(salt || ip || user_agent || day)
--
-- where `salt` is random, belongs to exactly one UTC day, and is thrown
-- away when the day rolls (`analytics_salt` holds one row, overwritten in
-- place). Once the salt for a day is gone the hashes from that day cannot
-- be re-derived from an IP or linked to any other day — they degrade into
-- opaque per-day counters. This is the Plausible construction.
--
-- `referrer_host` is the host only, never the full referring URL, which
-- can carry search terms or private path segments.
--
-- Africa/Abidjan is UTC+0, so the UTC `day` is also the local trading day
-- and no conversion is needed when lining views up against sessions.

CREATE TABLE IF NOT EXISTS pageviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc         TEXT    NOT NULL,
    day            TEXT    NOT NULL,          -- YYYY-MM-DD, UTC
    path           TEXT    NOT NULL,          -- no query string
    status         INTEGER NOT NULL,
    referrer_host  TEXT,                      -- host only; NULL when same-origin or absent
    visitor_hash   TEXT    NOT NULL,
    locale         TEXT,
    signed_in      INTEGER NOT NULL DEFAULT 0,
    plan           TEXT,                      -- 'free' | 'paid' | NULL when signed out
    is_pwa         INTEGER NOT NULL DEFAULT 0 -- launched from the installed app
);

CREATE INDEX IF NOT EXISTS ix_pageviews_day ON pageviews(day);
CREATE INDEX IF NOT EXISTS ix_pageviews_day_visitor ON pageviews(day, visitor_hash);
CREATE INDEX IF NOT EXISTS ix_pageviews_path ON pageviews(day, path);

-- Exactly one row, id = 1, overwritten when the day rolls. Deliberately
-- not a history: keeping old salts would make old hashes re-derivable.
CREATE TABLE IF NOT EXISTS analytics_salt (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    day   TEXT NOT NULL,
    salt  TEXT NOT NULL
);
