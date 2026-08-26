-- Cash-flow columns on `financials` (Phase 7).
--
-- Adds the three fields needed to compute P/FCF, FCF yield, and an
-- EV/EBITDA proxy without a full cash-flow statement schema:
--   cash_flow_ops    — "Flux de trésorerie liés à l'activité" (CFO)
--   capex            — capital expenditure (positive amount as reported)
--   free_cash_flow   — cash_flow_ops − capex, derived at extraction time
--                      so an operator can override either component
--                      without recomputing across the read path.
--
-- All three are optional REALs so filings that don't publish a
-- cash-flow table simply leave them NULL (matches the extraction
-- pipeline's null-on-absent contract).

ALTER TABLE financials ADD COLUMN cash_flow_ops   REAL;
ALTER TABLE financials ADD COLUMN capex           REAL;
ALTER TABLE financials ADD COLUMN free_cash_flow  REAL;
