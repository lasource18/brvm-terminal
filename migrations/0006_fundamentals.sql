-- Fundamentals — extracted financials, segments, and ownership (Phase 4b).
--
-- Populated by `services/extraction.py` from the `filings` corpus that 4a
-- built. All three tables key on `(ticker, period_year, period_kind)` so
-- re-extracting the same filing (a schema tweak, a corrected PDF, an
-- operator forcing a re-run) simply overwrites the prior rows. `filing_id`
-- carries the audit trail back to the source PDF.
--
-- All monetary amounts land in XOF unless the extractor is confident the
-- report was reported in a different currency (some sub-Saharan issuers
-- publish EUR or USD comparatives — the field is per-row so we don't have
-- to guess at write time).
--
-- `share_pct` on segments / ownership is 0-100, not 0-1. Kept as REAL
-- because segment maths in French annual reports rarely sum cleanly to 100
-- and we prefer to store what the model actually read.

CREATE TABLE IF NOT EXISTS financials (
    ticker              TEXT NOT NULL REFERENCES securities(ticker),
    period_year         INTEGER NOT NULL,       -- 2024, 2025, ...
    period_kind         TEXT NOT NULL,          -- 'annual' | 'H1' | 'Q1' | 'Q3' | 'other'
    currency            TEXT NOT NULL DEFAULT 'XOF',
    revenue             REAL,                   -- Chiffre d'affaires / Produit net bancaire
    operating_income    REAL,                   -- Résultat d'exploitation / RBE
    net_income          REAL,                   -- Résultat net (part du groupe si consolidé)
    total_assets        REAL,                   -- Total bilan
    total_equity        REAL,                   -- Capitaux propres (part du groupe)
    eps                 REAL,                   -- Bénéfice par action (XOF)
    dividend_per_share  REAL,                   -- Dividende proposé/payé (XOF)
    filing_id           INTEGER NOT NULL REFERENCES filings(id),
    extracted_utc       TEXT NOT NULL,
    PRIMARY KEY (ticker, period_year, period_kind)
);

CREATE INDEX IF NOT EXISTS ix_financials_ticker_year
    ON financials(ticker, period_year DESC);

CREATE TABLE IF NOT EXISTS financial_segments (
    ticker          TEXT NOT NULL REFERENCES securities(ticker),
    period_year     INTEGER NOT NULL,
    period_kind     TEXT NOT NULL,
    segment_kind    TEXT NOT NULL,              -- 'business' | 'geo'
    name            TEXT NOT NULL,              -- as reported ("Mobile Money", "Sénégal", ...)
    revenue         REAL,                       -- currency inherited from financials row
    share_pct       REAL,                       -- 0-100
    filing_id       INTEGER NOT NULL REFERENCES filings(id),
    extracted_utc   TEXT NOT NULL,
    PRIMARY KEY (ticker, period_year, period_kind, segment_kind, name)
);

CREATE INDEX IF NOT EXISTS ix_segments_ticker_year
    ON financial_segments(ticker, period_year DESC);

CREATE TABLE IF NOT EXISTS ownership (
    ticker          TEXT NOT NULL REFERENCES securities(ticker),
    period_year     INTEGER NOT NULL,
    period_kind     TEXT NOT NULL,
    holder          TEXT NOT NULL,              -- as reported ("SONATEL SA", "Public", "Flottant")
    share_pct       REAL,                       -- 0-100
    shares          INTEGER,                    -- when the report gives absolute counts
    filing_id       INTEGER NOT NULL REFERENCES filings(id),
    extracted_utc   TEXT NOT NULL,
    PRIMARY KEY (ticker, period_year, period_kind, holder)
);

CREATE INDEX IF NOT EXISTS ix_ownership_ticker_year
    ON ownership(ticker, period_year DESC);
