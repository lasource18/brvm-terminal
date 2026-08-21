-- Sub-cent precision for the LLM spend counter (Phase 3b).
--
-- `llm_spend.usd_cents` (0003) is too coarse for the actual per-call cost:
-- one Haiku batch over ~8 news items runs well under a tenth of a cent, so
-- integer-cent accumulation would round every call to 0 and the $1/day cap
-- would never see any spend at all.
--
-- We therefore accumulate in micro-dollars (1 USD = 1_000_000 micros) and
-- keep `usd_cents` as a rounded mirror for humans reading the table
-- (usd_cents = round(usd_micros / 10000)). The budget check reads
-- `usd_micros`.

ALTER TABLE llm_spend ADD COLUMN usd_micros INTEGER NOT NULL DEFAULT 0;

-- Backfill any pre-existing rows so the mirror stays consistent. Cheap:
-- this table holds at most one row per day and 3b is its first writer.
UPDATE llm_spend SET usd_micros = usd_cents * 10000 WHERE usd_micros = 0;
