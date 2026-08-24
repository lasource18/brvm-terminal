-- Company facts for the ratios engine (Phase 4d).
--
-- `shares_outstanding` and `float_pct` come from sikafinance's societe
-- page (`_SOCIETE_LABELS` in `sources/sikafinance.py`); we now persist
-- them instead of re-scraping on every tab render.
--
-- `market_cap_xof` is sikafinance's own published number at refresh time,
-- kept as a cross-check — the *live* market cap is always computed on the
-- fly from `shares_outstanding × latest_quote.last` so a price move is
-- reflected immediately without a re-refresh.
--
-- `company_facts_refreshed_utc` gates the weekly refresh job so a rerun
-- doesn't hammer sikafinance for rows already fresh.

ALTER TABLE securities ADD COLUMN shares_outstanding INTEGER;
ALTER TABLE securities ADD COLUMN float_pct REAL;
ALTER TABLE securities ADD COLUMN market_cap_xof REAL;
ALTER TABLE securities ADD COLUMN company_facts_refreshed_utc TEXT;
