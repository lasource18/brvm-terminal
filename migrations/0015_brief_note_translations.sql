-- PR-I: bilingual briefs + analyst notes.
--
-- The generator currently writes English markdown (the brief prompt
-- pins "your brief is in English"; the note prompt is similar). To
-- serve the FR/EN toggle without paying a translation cost on every
-- render, we cache a French translation alongside the English source.
--
-- `markdown` stays the canonical English source (backward-compatible —
-- existing rows already hold English). `markdown_fr` is populated by
-- a follow-up translation task in the same generation transaction; it
-- stays NULL when translation is skipped (budget cap, transport error)
-- and the read path falls back to `markdown` with a "translation
-- pending" badge on the UI.
--
-- `translation_generated_utc` distinguishes "never translated" (NULL)
-- from "translated at time X" so a future retry job can spot briefs
-- whose source is newer than their translation.

ALTER TABLE briefs ADD COLUMN markdown_fr TEXT;
ALTER TABLE briefs ADD COLUMN translation_generated_utc TEXT;

ALTER TABLE analyst_notes ADD COLUMN markdown_fr TEXT;
ALTER TABLE analyst_notes ADD COLUMN translation_generated_utc TEXT;
