-- PR-H: prospectus URL on `securities` + one-shot backfill.
--
-- Bloomberg-style bond overview wants a single "prospectus" link, not
-- a scrolling list of every admission communiqué. We already surface
-- matching `news_items` on the bond overview via
-- `_list_prospectus_news`; this column pins the canonical URL so the
-- template can render "Prospectus: <link>" as a first-class field and
-- degrade to the news list only when nothing is pinned.
--
-- Backfill strategy: for each bond issuer, seed `prospectus_url` from
-- the most recent `news_items` row that already matches the
-- `_list_prospectus_news` regex (issuer_name match + `obligat` /
-- `cotation` / `admission` in the title). Best-effort — issuers with
-- no matching news stay NULL and the template falls back to the
-- existing news list. Ordered newest-first via
-- `COALESCE(published_at, fetched_utc)` so bond re-listings pick up
-- the most recent admission communiqué, not the very first one from
-- 2015.

ALTER TABLE securities ADD COLUMN prospectus_url TEXT;

UPDATE securities
SET prospectus_url = (
    SELECT n.url
    FROM news_items AS n
    WHERE n.issuer_name = securities.issuer_name
      AND (
        LOWER(n.title) LIKE '%obligat%' OR
        LOWER(n.title) LIKE '%cotation%' OR
        LOWER(n.title) LIKE '%admission%'
      )
    ORDER BY COALESCE(n.published_at, n.fetched_utc) DESC
    LIMIT 1
)
WHERE kind = 'bond'
  AND issuer_name IS NOT NULL
  AND prospectus_url IS NULL;
