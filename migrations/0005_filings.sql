-- Fundamentals corpus (Phase 4a).
--
-- `filings`: one row per PDF report we've downloaded. Dedupe key is
-- `url_hash` (sha256 of the normalized source_url) so re-polling never
-- re-downloads. All extraction-related columns are NULL here — Phase 4b
-- fills `is_scanned`, `extracted_utc`, and writes to the fundamentals
-- tables that migration will introduce.
--
-- `filing_source_slugs`: persisted `(source, slug) → ticker` map. The
-- name-index resolver in `services/filings` writes a row on first sight
-- (ticker may be NULL for an unresolved slug so we don't keep re-trying
-- fuzzy matching every poll). A manual SQL insert can override any row.
--
-- `filings_spend`: the extractor's own daily counter, separate from
-- `llm_spend` (which stays for news tagging at $1/day). Annual reports
-- are 30-50k input tokens each — orders of magnitude bigger than a news
-- batch — so they get their own $2/day cap. Same micros-precision
-- accounting shape as `llm_spend` after 0004.

CREATE TABLE IF NOT EXISTS filings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL REFERENCES securities(ticker),
    issuer_name     TEXT,                   -- as displayed on the source
    doc_type        TEXT NOT NULL,          -- etats_financiers | rapport_annuel |
                                            -- rapport_activites | resultats |
                                            -- rse | assemblee | autre
    period_kind     TEXT,                   -- annual | H1 | Q1 | Q3 | other | NULL
    period_year     INTEGER,                -- 2024, 2025, ...
    period_label    TEXT,                   -- raw label from source ("Exercice 2024", "1er semestre 2026")
    source          TEXT NOT NULL,          -- 'brvm_org' | 'sikafinance'
    source_url      TEXT NOT NULL,
    url_hash        TEXT NOT NULL UNIQUE,   -- sha256(normalized source_url)
    published_date  TEXT,                   -- 'YYYY-MM-DD' from filename when available
    file_path       TEXT NOT NULL,          -- relative to project root, e.g. 'data/filings/SNTS/…'
    size_bytes      INTEGER NOT NULL,
    sha256          TEXT NOT NULL,          -- of the downloaded bytes; a second URL for the same content is still a distinct row
    page_count      INTEGER,                -- via pypdf; NULL if the probe fails
    is_scanned      INTEGER,                -- 0/1; NULL until Phase 4b probes for extractable text
    fetched_utc     TEXT NOT NULL,
    extracted_utc   TEXT                    -- Phase 4b stamps this once financials/ownership/segments rows land
);

CREATE INDEX IF NOT EXISTS ix_filings_ticker         ON filings(ticker);
CREATE INDEX IF NOT EXISTS ix_filings_doc_type       ON filings(doc_type);
CREATE INDEX IF NOT EXISTS ix_filings_published_date ON filings(published_date DESC);
-- Fast lookup for the 4b extractor's "which filings still need me?" query.
CREATE INDEX IF NOT EXISTS ix_filings_unextracted    ON filings(extracted_utc) WHERE extracted_utc IS NULL;

CREATE TABLE IF NOT EXISTS filing_source_slugs (
    source          TEXT NOT NULL,          -- 'brvm_org' | 'sikafinance'
    slug            TEXT NOT NULL,          -- as it appears on the source (URL fragment)
    ticker          TEXT REFERENCES securities(ticker),
                                            -- NULL means "resolver has seen this slug and cannot map it"
    display_name    TEXT,                   -- what the source called this issuer, for auditing
    resolved_utc    TEXT NOT NULL,
    note            TEXT,                   -- optional operator note, e.g. 'manual override'
    PRIMARY KEY (source, slug)
);

CREATE TABLE IF NOT EXISTS filings_spend (
    day             TEXT PRIMARY KEY,       -- 'YYYY-MM-DD' UTC
    calls           INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    usd_micros      INTEGER NOT NULL DEFAULT 0,
    usd_cents       INTEGER NOT NULL DEFAULT 0     -- rounded mirror of usd_micros for humans
);
