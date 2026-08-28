-- Cash-flow recovery tried-and-failed stamp (F-25).
--
-- `reset_missing_cashflow` clears `extracted_utc` on filings whose
-- `financials` row is missing every cash-flow column, so the next
-- extraction pass re-processes them with the Phase-7-aware prompt.
-- Without a persistent stamp, filings whose cash-flow statement sits
-- past the 120k-char truncation (or doesn't exist at all) are queued
-- again on every recovery run — burning ~30-50k tokens per re-extract
-- against the daily $2 cap.
--
-- The stamp records when a filing was included in a recovery pass so
-- future runs of `reset_missing_cashflow` skip it. Operators who
-- upgrade the extraction prompt can clear this column manually
-- (`UPDATE filings SET cashflow_recovery_attempted_utc = NULL WHERE ...`)
-- to force a retry batch.

ALTER TABLE filings ADD COLUMN cashflow_recovery_attempted_utc TEXT;
