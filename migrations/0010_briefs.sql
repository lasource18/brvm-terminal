-- Daily brief (Phase 6b).
--
-- One row per calendar date (UTC). Re-running the generator for the same
-- day overwrites the row via `INSERT OR REPLACE` in the store — the last
-- run wins, which matches how "daily brief" reads to a human (there is
-- only one for a given day). Cost accounting for the brief writer lives
-- in its own `brief_spend` table so it can't compete with the 3b tagger
-- ($1/day) or the 4b extractor ($2/day) for the same budget line.
--
-- Markdown is stored as-is; the /brief page renders it server-side via
-- markdown-it-py. `context_json` is the structured snapshot the model
-- read (movers, news, upcoming CA) so a future re-run can regenerate
-- with a different prompt without re-gathering the data.

CREATE TABLE IF NOT EXISTS briefs (
    day              TEXT PRIMARY KEY,       -- 'YYYY-MM-DD' UTC
    model            TEXT NOT NULL,          -- claude-haiku-4-5-20251001 / -sonnet-4-6 / ...
    title            TEXT,                   -- optional one-liner subject
    markdown         TEXT NOT NULL,          -- body content
    context_json     TEXT NOT NULL,          -- snapshot of inputs (for reproducibility)
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    usd_micros       INTEGER NOT NULL DEFAULT 0,
    generated_utc    TEXT NOT NULL,
    session_date     TEXT                    -- ISO date this brief covers (typically = day)
);

CREATE INDEX IF NOT EXISTS ix_briefs_day ON briefs(day DESC);

-- Separate daily counter for the brief writer. Same shape as llm_spend /
-- filings_spend after 0004; SpendTable already reads three names.
CREATE TABLE IF NOT EXISTS brief_spend (
    day             TEXT PRIMARY KEY,       -- 'YYYY-MM-DD' UTC
    calls           INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    usd_cents       INTEGER NOT NULL DEFAULT 0,
    usd_micros      INTEGER NOT NULL DEFAULT 0
);
