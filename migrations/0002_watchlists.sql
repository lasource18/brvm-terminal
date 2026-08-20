-- Watchlists (multiple named lists from day one).
-- Single-user app, no auth. Each watchlist has a slug (URL-safe key) and
-- a display name. Items reference `securities(ticker)` with a cascade
-- delete so removing a list wipes its items.

CREATE TABLE IF NOT EXISTS watchlists (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_utc  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    watchlist_id INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    ticker       TEXT    NOT NULL REFERENCES securities(ticker),
    sort_order   INTEGER NOT NULL DEFAULT 0,
    added_utc    TEXT    NOT NULL,
    PRIMARY KEY (watchlist_id, ticker)
);

CREATE INDEX IF NOT EXISTS ix_watchlist_items_wl ON watchlist_items(watchlist_id);

-- Seed a starting list so first-run users have a place to add tickers.
INSERT INTO watchlists (slug, name, sort_order, created_utc)
SELECT 'default', 'Default', 0, datetime('now')
WHERE NOT EXISTS (SELECT 1 FROM watchlists WHERE slug = 'default');
