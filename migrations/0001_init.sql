-- brvm-terminal initial schema.
-- All timestamp columns store UTC ISO-8601 strings; session_date is the
-- Africa/Abidjan calendar date of a trading session (YYYY-MM-DD).

CREATE TABLE IF NOT EXISTS securities (
    ticker          TEXT PRIMARY KEY,
    isin            TEXT,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('equity','index','bond')),
    country         TEXT,
    sector          TEXT,
    currency        TEXT NOT NULL DEFAULT 'XOF',
    source_url      TEXT,
    first_seen_utc  TEXT NOT NULL,
    last_seen_utc   TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_securities_kind ON securities(kind);
CREATE INDEX IF NOT EXISTS ix_securities_country ON securities(country);

CREATE TABLE IF NOT EXISTS quote_snapshots (
    ticker          TEXT NOT NULL REFERENCES securities(ticker),
    captured_utc    TEXT NOT NULL,
    source          TEXT NOT NULL,
    last            REAL,
    prev_close      REAL,
    open            REAL,
    high            REAL,
    low             REAL,
    volume          INTEGER,
    turnover        REAL,
    change_abs      REAL,
    change_pct      REAL,
    is_stale        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ticker, captured_utc, source)
);

CREATE INDEX IF NOT EXISTS ix_quote_snapshots_captured ON quote_snapshots(captured_utc);

CREATE TABLE IF NOT EXISTS daily_bars (
    ticker          TEXT NOT NULL REFERENCES securities(ticker),
    session_date    TEXT NOT NULL,
    open            REAL,
    high            REAL,
    low             REAL,
    close           REAL NOT NULL,
    volume          INTEGER,
    turnover        REAL,
    source          TEXT NOT NULL,
    ingested_utc    TEXT NOT NULL,
    PRIMARY KEY (ticker, session_date)
);

CREATE TABLE IF NOT EXISTS index_levels (
    ticker          TEXT NOT NULL REFERENCES securities(ticker),
    session_date    TEXT NOT NULL,
    level           REAL NOT NULL,
    change_pct      REAL,
    source          TEXT NOT NULL,
    ingested_utc    TEXT NOT NULL,
    PRIMARY KEY (ticker, session_date)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    target          TEXT NOT NULL,
    started_utc     TEXT NOT NULL,
    finished_utc    TEXT,
    status          TEXT NOT NULL,
    http_status     INTEGER,
    rows_written    INTEGER,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS ix_fetch_log_started ON fetch_log(started_utc);
