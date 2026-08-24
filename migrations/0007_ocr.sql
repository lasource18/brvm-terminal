-- OCR bookkeeping for scanned filings (Phase 4c).
--
-- Phase 4b probes each PDF with pypdf and stamps `is_scanned=1` when no
-- text comes out. Phase 4c runs those through ocrmypdf so they can be
-- re-extracted. The one bit of state we need is "did we already attempt
-- OCR on this row?", otherwise a failing PDF would be retried every night.
--
-- Successful OCR clears `is_scanned` and the extractor's `extracted_utc`
-- stamp so the row re-enters `list_needing_extraction`. Failing OCR just
-- stamps `ocr_attempted_utc` and leaves `is_scanned=1` — the file stays
-- on disk for a future operator retry (drop `ocr_attempted_utc` and it'll
-- be picked up again).

ALTER TABLE filings ADD COLUMN ocr_attempted_utc TEXT;

CREATE INDEX IF NOT EXISTS ix_filings_pending_ocr
    ON filings(ocr_attempted_utc)
    WHERE is_scanned = 1 AND ocr_attempted_utc IS NULL;
