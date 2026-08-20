-- News feed + corporate actions (Phase 3a).
--
-- news_items: raw ingested articles / communiqué PDFs. Dedupe key is
-- url_hash (sha256 of the normalized URL + title). LLM-tagged fields
-- (tickers, relevance, category_llm, summary_fr, summary_en) are filled
-- by the Phase 3b worker; nullable here so ingest can land first.
--
-- corporate_actions: structured events with a date + ticker. Keyed on
-- (ticker, kind, ex_date) so re-parsing an already-known dividend is a
-- no-op. `ex_date` may be NULL for "A préciser" entries; the (ticker,
-- kind) pair still deduplicates those.
--
-- llm_spend: daily counter used by Phase 3b to enforce the $1/day cap.
-- Populated by the tagging worker; created now so migrations stay
-- append-only across sub-phases.

CREATE TABLE IF NOT EXISTS news_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,          -- 'sikafinance', 'brvm_org'
    kind            TEXT NOT NULL,          -- 'news' | 'communique'
    url             TEXT NOT NULL,
    url_hash        TEXT NOT NULL UNIQUE,   -- sha256(url + '|' + normalized_title)
    title           TEXT NOT NULL,
    chapeau         TEXT,                   -- lead paragraph (news) or NULL
    issuer_name     TEXT,                   -- best-effort company name from the row
    ticker_hint     TEXT,                   -- best-effort ticker resolved at ingest (nullable FK)
    published_at    TEXT,                   -- ISO-8601 UTC when known; date-only otherwise
    fetched_utc     TEXT NOT NULL,
    -- Phase 3b (LLM tagging) fills these:
    tickers_llm     TEXT,                   -- CSV of tickers from Haiku
    relevance       INTEGER,                -- 0..10
    category_llm    TEXT,                   -- earnings|dividend|governance|macro|capital_action|other
    summary_fr      TEXT,
    summary_en      TEXT,
    tagged_utc      TEXT
);

CREATE INDEX IF NOT EXISTS ix_news_items_published ON news_items(published_at DESC);
CREATE INDEX IF NOT EXISTS ix_news_items_ticker_hint ON news_items(ticker_hint);
CREATE INDEX IF NOT EXISTS ix_news_items_source_kind ON news_items(source, kind);
CREATE INDEX IF NOT EXISTS ix_news_items_untagged ON news_items(tagged_utc) WHERE tagged_utc IS NULL;

CREATE TABLE IF NOT EXISTS corporate_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL REFERENCES securities(ticker),
    kind            TEXT NOT NULL,          -- dividend|agm|rights|split|admission|other
    ex_date         TEXT,                   -- 'YYYY-MM-DD' or NULL for TBD
    pay_date        TEXT,
    amount          REAL,                   -- per-share amount in `currency`
    currency        TEXT,                   -- 'XOF' typical
    yield_pct       REAL,                   -- as displayed on source, if provided
    note            TEXT,                   -- free-form (e.g. 'A préciser', 'Exercice 2025')
    source          TEXT NOT NULL,          -- 'sikafinance', ...
    source_url      TEXT,
    first_seen_utc  TEXT NOT NULL,
    last_seen_utc   TEXT NOT NULL,
    UNIQUE (ticker, kind, ex_date)
);

CREATE INDEX IF NOT EXISTS ix_corporate_actions_ticker ON corporate_actions(ticker);
CREATE INDEX IF NOT EXISTS ix_corporate_actions_ex_date ON corporate_actions(ex_date);

CREATE TABLE IF NOT EXISTS llm_spend (
    day             TEXT PRIMARY KEY,       -- 'YYYY-MM-DD' UTC
    calls           INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    usd_cents       INTEGER NOT NULL DEFAULT 0
);
