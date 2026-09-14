-- Flag a pageview whose request headers do not look like a browser.
--
-- The name-based filter in `services/analytics._BOTS` only catches
-- crawlers we already know about, so an unnamed one silently inflated the
-- numbers and there was no way to notice — the user agent is deliberately
-- not stored, so it could not be investigated after the fact either.
--
-- Rows are now FLAGGED rather than dropped. A heuristic that is wrong
-- then costs visibility, not data: the row is still there to be counted
-- once the rule is corrected. Headline figures exclude suspected rows and
-- report how many they excluded, so distortion is visible instead of
-- invisible.

ALTER TABLE pageviews ADD COLUMN is_suspected_bot INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_pageviews_day_human
    ON pageviews(day, is_suspected_bot);
