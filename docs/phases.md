# brvm-terminal — phase log

Living record of what's been shipped, what was cut/deferred, and what
comes next. Updated at the end of each phase. See `CLAUDE.md` for the
project charter and `README.md` for the current "Try it" quickstart.

| Phase | Title | Status | Completed |
|-------|-------|--------|-----------|
| 0 | Scaffold | done | 2026-08-18 |
| 1 | Reference data + quotes | done | 2026-08-19 |
| 2 | Web UI v1 (HTMX) | done | 2026-08-19 |
| 2.5 | Search + directory + company tab shell | done | 2026-08-19 |
| 3a | News + corporate actions — ingest | done | 2026-08-20 |
| 3b | News + corporate actions — Haiku tagging ($1/day cap) | done | 2026-08-21 |
| 3c | News + corporate actions — UI (news feed, per-ticker tabs, 30-day strip on /) | done | 2026-08-21 |
| 4a | Fundamentals — filings corpus + storage | done | 2026-08-21 |
| 4b | Fundamentals — Haiku extraction + Financials/Ownership/Segments tabs | done | 2026-08-22 |
| 4c | Fundamentals — OCR + interim extraction + sikafinance-communiqué fallback | done | 2026-08-23 |
| 4d | Fundamentals — financial ratios on the Financials + Peers tabs | done | 2026-08-24 |
| 5 | TUI (Textual) | not started | — |
| 6a | Alerts — price-move / new-filing / news rules + Discord delivery | done | 2026-08-24 |
| 6b | Daily brief (post-close, Haiku) | done | 2026-08-25 |
| 6c | Analyst-note synthesis (weekly per-ticker) | not started | — |

**Backlog** (raised during earlier phases; each becomes its own mini-phase
when we pick it up):
- **Bond ingestion.** Equities + indices auto-appear on the AAZ page and
  are picked up by `sources/sikafinance.parse_aaz` — bonds are on a
  different page and no parser exists yet. `SecurityKind` already allows
  `"bond"`; needs a source survey + parser + `kind="bond"` branch. Flag
  raised in Phase 1's notes.
- **Cash-flow extraction (unlocks P/FCF, FCF yield, EV/EBITDA).** Phase
  4d ships every ratio computable from the current P&L + balance-sheet
  extract. Adding `cash_flow_ops`, `capex`, and a derived
  `free_cash_flow` to `services/extraction` + `financials` unlocks the
  cash-flow-adjacent ratios and a proper EV multiple. One-off Haiku
  re-run against the ~200 extracted filings; keeps the same $2/day cap.
  Flagged in 4d's writeup.
- **Palmarès parser + BOC row extraction.** Deferred from Phase 1.
- **`hx-push-url` on the `/news` filter form** so filtered views become
  shareable links. Small polish, not currently needed.
- **~~Refactor `_RELOADABLE` in `tests/conftest.py` toward
  dependency-injected settings~~ done in Phase 6a** — replaced by a lazy
  proxy in `brvm/config.py` with `reset_settings_cache()` as the test
  escape hatch. `_RELOADABLE` is gone; every service accesses settings
  via the proxy at call time.
- **SGBC dividend fixture (in `test_news_service.py`) drifts out of the
  60-day window** on 2026-08-24 — pre-existing failure not related to
  6a. Fix is refreshing `tests/fixtures/sikafinance/dividendes.html`.

---

## Phase 0 — Scaffold (done 2026-08-18)

**Delivered**
- uv-managed Python 3.12 project, ruff + pytest + mypy configured.
- Src layout matching CLAUDE.md architecture (`sources/`, `store/`,
  `services/`, `jobs/`, `apps/web/`).
- SQLite schema in `migrations/0001_init.sql` (6 tables:
  `securities`, `quote_snapshots`, `daily_bars`, `index_levels`,
  `fetch_log`, plus `_schema_migrations`); `scripts/migrate.py` applies
  it idempotently and enables WAL + FK.
- `Justfile` with `sync`, `dev`, `migrate`, `test`, `lint`, `fmt`,
  `refresh-fixtures`, `snapshot` targets. Web dev port `8765` (avoids a
  local project on 8000).
- Skeleton `docker-compose.yml`, `deploy/brvm-terminal.service`,
  `deploy/Caddyfile.example` — not run yet, kept for shape review.
- FastAPI stub at `/` (dark terminal aesthetic) and `/health`.
- `env.example` (renamed from `.env.example` to satisfy local `.env*`
  write-block); `.gitignore` covers venv/data/sqlite artifacts.

**Definition of Done — met**
- `just sync` → `.venv` populated.
- `just migrate` → `data/brvm.sqlite` with all tables.
- `just test` → all green (12 tests at phase end).
- `just dev` → `curl http://127.0.0.1:8765/health` returns JSON `ok`.
- `ruff check .` clean.
- README documents install + run in ≤ 6 commands.

**Notes / follow-ups**
- Homebrew installed `uv 0.12.5`, `just 1.58.0`, `python@3.12` (was not
  present on the Mac).
- Starlette 1.6 requires the new `TemplateResponse(request, template,
  ctx)` signature — noted for future template views.

---

## Phase 1 — Reference data + quotes (done 2026-08-19)

**Delivered**
- Live HTML/PDF fixtures committed under `tests/fixtures/{sikafinance,afx,brvm_org}/`
  captured on 2026-08-18. `scripts/refresh_fixtures.py` re-fetches them
  from the network (dev-only, never from CI).
- Parsers as pure functions (HTML str → typed models):
  - `sources/sikafinance.py`: `parse_aaz`, `parse_cotation`,
    `parse_cotation_meta`, `parse_historique` (+ fetchers).
  - `sources/afx_kwayisi.py`: `parse_home`, `parse_ticker_page`.
  - `sources/brvm_org.py`: `resolve_boc_pdf_url`, `parse_boc_pdf_date`.
- Shared `sources/_num.py` for French / mixed number formats (nbsp
  thousands, comma decimal, mixed period-decimal percentages) and
  `sources/_http.py` for the polite httpx client (custom UA, redirects,
  timeout from `.env`).
- Store repos: `store/securities.py`, `store/quotes.py` (upserts for
  securities/daily_bars/index_levels, snapshot inserts, fetch_log helpers,
  `latest_snapshot_by_ticker` query for the demo).
- `services/providers.py` with `QuoteProvider` protocol, `ScrapeProvider`
  (sikafinance + afx.kwayisi cross-check), and `ApiProvider` stub gated by
  `BRVM_API_KEY` (raises `NotImplementedError` with a message pointing at
  the deprecated Apify feed).
- `services/quotes.py` — `snapshot_once()` and `top_by_turnover()` used by
  the demo command and, later, the UI.
- `jobs/scheduler.py` — APScheduler build with Africa/Abidjan tz, a
  10-min market-hours job, and an hourly outside-hours job.
- `jobs/quote_snapshot.py` — `just snapshot` entry point; prints the
  top-10-by-turnover table.

**Definition of Done — met**
- `just snapshot` completes end-to-end against the live web.
- `select count(*) from securities` = 69 (48 equities + 21 indices;
  target was ≥ 45).
- `select count(*) from quote_snapshots` = 48 (target was > 0).
- 55 fixture-based tests green, ruff clean, no network in the test run.
- README "Try it" section shows the `just snapshot` command + sample
  output.

**Key discovery**
- Sikafinance ticker URLs need the country-code suffix
  (`.sn`, `.ci`, `.bf`, `.bj`, `.ml`, `.ne`, `.tg`), not a single
  hardcoded suffix. Detected from the `<img src="/i/<cc>.png">` flag in
  the `.quotebarE` header and stored on `securities.country`.

**Deferred (flagged, not blocking Phase 2)**
- Sikafinance `parse_palmares` — the fixture is committed but the parser
  hasn't been written; the aaz page already yields sortable data for the
  Phase 2 gainers/losers view.
- BOC PDF table extraction — only the URL resolver + a smoke test for
  pypdf openability landed. Full row extraction stays a cross-check task
  for after Phase 2 signals what accuracy we actually need.
- Bond ingestion — no source page surveyed. Proposed Phase 1.5 mini-task
  once equity flow is stable.
- Scheduler is wired but not started by the FastAPI app; Phase 2 will
  boot it as a background scheduler alongside uvicorn.

---

## Phase 2 — Web UI v1 (HTMX) (done 2026-08-19)

**Delivered**
- Dark, dense, monospace terminal aesthetic: `apps/web/static/style.css`
  extracted from inline styles; extended with tiles, panels, quote
  tables, badges, and per-page bits (security head, watchlist actions).
- Full-page views:
  - `GET /` — market overview: 3 index tiles (BRVMC / BRVM30 / BRVMPR) +
    turnover leaders / gainers / losers panels; auto-refresh via HTMX
    every 5 min during market hours (every 15 min otherwise).
  - `GET /s/{ticker}` — big price, metadata, Lightweight Charts candles
    + volume histogram fed from `/api/history/{ticker}`.
  - `GET /watchlists` — index of lists with a create form.
  - `GET /watchlists/{slug}` — quote board for one list with add/remove.
- HTMX fragment endpoints under `/_frag/*` (overview, watchlists index,
  add/remove item). No websockets, no SPA framework.
- JSON API `GET /api/history/{ticker}` returning ascending OHLCV bars
  (Lightweight Charts format).
- New migration `migrations/0002_watchlists.sql` with `watchlists` +
  `watchlist_items` tables (multiple named lists from day one) and a
  seeded `default` list; cascade delete on list removal.
- Services layer: `services/market.py` (overview, gainers/losers,
  indices, get_security), `services/history.py` (15-min in-memory TTL
  + write-through to `daily_bars`, DB fallback on network error),
  `services/watchlist.py` (CRUD + `get_with_quotes` joining latest
  snapshot per item), `services/_view.py` (typed view models).
- FastAPI split into `routes/{pages,fragments,api}.py` + shared
  `_common.py`; `apps/web/main.py` mounts `/static`, includes routers,
  and boots the APScheduler in a `lifespan` context (both jobs from
  Phase 1 register automatically).
- `python-multipart` added for `Form(...)` support.

**Definition of Done — met**
- `just dev` boots web + scheduler in one process.
- `curl /` returns HTML containing `BRVM COMPOSITE`, `Gainers`, `Losers`,
  `Turnover leaders`, and populated ticker rows.
- `/s/SNTS` renders and the chart pulls 64 candles fetched on demand
  from sikafinance (May → August 2026); cache hit on second load.
- `/watchlists` shows the seeded `Default` list; create + add + remove
  round-trip through HTMX in tests.
- 81 tests green (26 new: pages, fragments, market, history, watchlist
  repo, scheduler lifespan). Ruff clean.

**Notes / follow-ups**
- Overview refresh cadence is 60s during market hours, 300s after close
  (tuned after Phase 2 shipped — original 300s/900s felt static). Same
  cadence now applied to the watchlist page too. Every polled fragment
  shows a `generated <UTC>` timestamp + `auto-refresh every Xs` line so
  it's obvious the poll is firing.
- Charts run against Lightweight Charts 4.2.1 from unpkg CDN; no offline
  bundling yet — fine on the MacBook, would need vendoring for the VPS
  if we want the site to load without egress.
- Test isolation relies on `importlib.reload` of every module that
  captures `settings` at import. A little heavy-handed; if this list
  keeps growing, we should refactor to dependency-inject the settings.
- Palmarès parser and BOC row extraction remain deferred (Phase 1
  follow-ups).

---

---

## Phase 2.5 — Search + directory + company tab shell (done 2026-08-19)

**Delivered**
- **Topbar search** with HTMX autocomplete over `securities.ticker +
  name`. Debounced 150 ms, dropdown shows up to 8 hits; Enter jumps to
  the first result (small handler in `app.js`). Ranking: exact ticker >
  ticker prefix > name LIKE.
- **`/directory` page** — full securities table with HTMX-live filters
  for country, sector, kind (equity/index/bond) and free text. Body
  fragment served from `/_frag/directory`. Links to `/s/{ticker}`.
- **Tab shell on `/s/{ticker}`** at `/s/{ticker}/{tab}`:
  - `/s/{ticker}` 307-redirects to `/overview` (deep-link stable).
  - **Overview** — chart moved into `_tab/overview.html`.
  - **Description** — company profile from sikafinance
    `/marches/societe/<T>.<cc>` (description, address, phone, leadership,
    shares outstanding, float, market cap, main shareholders as name/%
    pairs). Falls back to afx.kwayisi `<div data-fact>` if sikafinance
    is down.
  - **Peers** — sector list from sikafinance `/marches/secteur/<T>.<cc>`
    (peer ticker, last, day%, YTD%, volume, self excluded). Falls back
    to the afx.kwayisi `<table data-comp>` peers block.
  - **News / Corporate actions / Financials / Ownership / Segments** —
    placeholder tabs that render "Coming in Phase 3/4" so links stay
    live. Registry in `src/brvm/apps/web/tabs.py`.
- New services: `services/company.py` (60-min in-memory TTL for
  description + peers, sikafinance→afx fallback), `services/search.py`,
  `services/directory.py`.
- New view models in `services/_view.py`: `SearchHit`, `DirectoryRow`,
  `CompanyProfile`, `Shareholder`, `PeerRow`, `PeersView`.
- New parsers: sikafinance `parse_societe` + `parse_secteur` (+ fetchers);
  afx.kwayisi `parse_factsheet` + `parse_competitors`.
- Two new committed fixtures (`sikafinance/societe_SNTS.html`,
  `sikafinance/secteur_SNTS.html`) + `scripts/refresh_fixtures.py`
  updated to keep them fresh.

**Definition of Done — met**
- 120 tests green, ruff clean.
- Live: `curl /_frag/search?q=son` returns the SNTS · SONATEL card.
- Live: `/directory?country=SN` narrows to 3 SN-issued securities.
- Live: `/s/SNTS` 307 → `/s/SNTS/overview`.
- Live: `/s/SNTS/description` shows the SONATEL description, Wagane
  Diouf address, Alioune NDIAYE leadership, FRANCE TELECOM shareholder.
- Live: `/s/SNTS/peers` lists ONTBF + ORAC in the BRVM -
  TELECOMMUNICATIONS sector (SNTS excluded).
- Live: `/s/SNTS/financials` renders the "Coming in Phase 4" placeholder.

**Notes / follow-ups**
- Sikafinance `societe_SNTS.html` also carries a 5-year financials
  snippet (revenue, net income, EPS, P/E, dividend). Not parsed here —
  belongs in Phase 4 where we build the Financials tab properly.
- Test isolation now reloads three extra service modules on every
  `client` fixture: company, search, directory. The reload list in
  `tests/conftest.py::_RELOADABLE` continues to grow; if it hits ~15
  modules we should refactor to dependency-injected settings.

---

---

## Phase 3a — News + corporate actions — ingest (done 2026-08-20)

**Delivered**
- Migration `0003_news.sql`: `news_items` (source, kind,
  url, url_hash UNIQUE, title, chapeau, issuer_name, ticker_hint,
  published_at, fetched_utc + nullable LLM-tag columns for 3b);
  `corporate_actions` (ticker FK, kind, ex_date, pay_date, amount,
  currency, yield_pct, note, source, source_url); `llm_spend` (day
  PRIMARY KEY, calls, tokens, usd_cents) — created now so 3b's budget
  counter has a home without another migration.
- Sikafinance parsers (fixtures captured 2026-08-20):
  - `parse_news_feed` — `ul.news-feed > li.news-item` (title, chapeau,
    `time[datetime]` → ISO-8601 UTC, Africa/Abidjan being UTC+0).
  - `parse_communiques` — `table.tbl100_6` PDF rows, splits
    `"COMPANY : TITLE"` into `issuer_name` + `title`.
  - `parse_dividendes` — `table#tbdDiv` upcoming dividend calendar
    (ticker via `/marches/cotation_TICKER.cc` link, amount, yield;
    "A préciser" rows keep `ex_date=NULL` with the raw string in `note`).
- `sources/_dedupe.news_hash(url, title)` — sha256 over normalized URL
  (lowercased, query/fragment/trailing-slash stripped) + collapsed title.
  Same helper is reused by the future brvm.org / afx parsers.
- `store/news.py`: `upsert_news_items` (INSERT ... ON CONFLICT DO NOTHING
  keyed on `url_hash`, so LLM-tagged fields set later are never
  clobbered), `upsert_corporate_actions` (pre-check + branch, because
  SQLite's UNIQUE lets duplicate NULL `ex_date` rows through), plus
  `list_news(ticker=…)` matching both `ticker_hint` and CSV
  `tickers_llm`, and `list_corporate_actions_upcoming(days=30)`.
- `services/news.poll_all()` — one-shot fetch of feed + communiqués +
  dividends, resolves `ticker_hint` best-effort against
  `securities.name`, drops dividend rows for unknown tickers (FK safety)
  and logs the skip. Returns row-count dict for scheduler / demo.
- Scheduler adds `news_market_hours` (mon-fri, 09-14 Abidjan, every 15
  min) and `news_hourly_outside` jobs alongside the existing snapshot
  jobs.
- `jobs/news_poll.py` + `just news-poll` — one-shot poll that also
  prints the 5 latest news items and the next-30-day corporate-actions
  calendar (mirrors the Phase 1 `just snapshot` demo shape).

**Definition of Done — met**
- `just migrate` picks up `0003_news.sql` idempotently.
- `just news-poll` against live sikafinance: ingested 10 news + 30
  communiqués + 11 dividends first pass; re-run reports 0 new, 10/30
  duplicates and 11 dividend updates (idempotent).
- 143 tests green (23 new): sikafinance news parsers, `_dedupe`
  normalization, news repo dedupe / corporate-actions upsert / upcoming
  window / ticker filter, services poll_all end-to-end + unknown-ticker
  degradation, scheduler wiring.
- Ruff clean.

**Notes / follow-ups**
- brvm.org `/en/actualites` is a static Drupal welcome page, not a
  structured feed — deferred until we find (or ask the user for) a real
  BRVM.org announcements URL. sikafinance already covers news +
  communiqués + dividends end-to-end.
- Dividend-calendar rows with ex_date "A préciser" (~half the fixture)
  land as `ex_date NULL` and are deduped by a pre-check because SQLite
  UNIQUE(ticker, kind, ex_date) treats each NULL as distinct.
- `dividends_updated` counts all rows on every poll — we don't diff
  before UPDATE. Fine at this volume (~10 rows / poll); revisit if it
  ever becomes chatty for change-tracking.
- Ticker resolution on communiqués is exact-name-match against
  `securities`; ~half of common issuers resolve (SGBCI, TOTAL CI, SAPH
  CI, ETI TG). Everything else stays `ticker_hint=NULL` and will be
  attributed by the 3b LLM tagger writing `tickers_llm`.

---

## Phase 3b — News + corporate actions — Haiku tagging (done 2026-08-21)

**Delivered**
- `services/llm.py` — thin Anthropic client for one call shape only:
  tag a batch of news items.
  - Model `claude-haiku-4-5-20251001` (charter), overridable via
    `ANTHROPIC_MODEL`. Dated snapshot on purpose so an alias re-point
    can't change tagging behaviour or cost under us.
  - **Structured output** via `output_config.format` + a JSON schema
    (`{results: [{id, tickers[], relevance 0-10, category enum,
    summary_fr, summary_en}]}`), so there is no fenced-block /
    regex-extraction step. Shape is guaranteed by the schema; *meaning*
    is still validated locally — hallucinated ids dropped, tickers
    filtered against the live universe, relevance clamped, unknown
    category coerced to `other`.
  - **Retry-on-parse-failure**: one corrective retry that feeds the bad
    reply back. A `max_tokens` truncation or a refusal raises instead —
    a verbatim retry can't fix either.
  - `LLMResponseError` carries the `Usage` billed so far, so a failed
    batch still lands in the budget counter.
  - Exact cost accounting: per-model price table ($1/$5 per MTok for
    Haiku 4.5), cache reads at 0.1x and writes at 1.25x.
  - The `anthropic` import is lazy — the web app imports the scheduler
    on every boot and shouldn't pay for the SDK unless a pass runs.
- `services/tagging.py` — the incremental worker. `tag_pending()` pulls
  `tagged_utc IS NULL` rows, batches them (default 8), checks the budget
  before every batch, records real cost straight after every call, and
  writes `tickers_llm` / `relevance` / `category_llm` / `summary_fr` /
  `summary_en`. Returns a counts dict; never raises for the operational
  cases (no key, budget spent, API down).
- `store/spend.py` — the daily counter. `add_usage` / `spent_micros` /
  `remaining_micros`.
- Migration `0004_llm_spend_micros.sql` — adds `llm_spend.usd_micros`.
  0003's `usd_cents` alone is too coarse: one batch costs well under a
  cent, so integer-cent accumulation rounds every call to 0 and the cap
  never sees any spend. `usd_cents` is kept as a rounded mirror.
- `store/news.py` — `list_untagged`, `count_untagged`, `apply_tags`
  (normalizes + dedupes the ticker CSV so `list_news(ticker=…)` matches).
- `jobs/news_tag.py` + `just news-tag` / `just news-tag-dry`.
- Scheduler: `news_tag_market_hours` (mon-fri 09-14 Abidjan, `7-59/15`)
  and `news_tag_hourly_outside` (:31) — trailing each news poll by ~7
  min so a cycle ingests then tags.
- Settings: `ANTHROPIC_MODEL`, `LLM_DAILY_CAP_CENTS` (100),
  `LLM_BATCH_SIZE` (8), `LLM_MAX_OUTPUT_TOKENS`,
  `LLM_MAX_CONSECUTIVE_FAILURES`; `settings.has_llm`.
- `tests/_fake_anthropic.py` — scripted stand-in for the SDK so the whole
  pipeline is testable offline.

**Two invariants the tests pin**
1. **Never re-process an article.** Every item in a successful call gets
   `tagged_utc` stamped — including ones the model returned nothing for
   (they land with NULL tags). Otherwise one item the model keeps
   ignoring is billed on every pass, forever.
2. **Hard $1/day cap.** Checked before each batch against accumulated
   `llm_spend.usd_micros`, and the real cost of each call is committed
   immediately, so a crash mid-pass can't lose spend. Once crossed the
   worker no-ops with a warning until UTC midnight.

**Definition of Done — met**
- `just migrate` picks up `0004` idempotently (second run skips it).
- End-to-end over the committed fixtures (40 real news + communiqué rows
  from the 2026-08-20 capture): 5 batches, 40 tagged, `pending_after=0`,
  spend recorded in `llm_spend`; a second `just news-tag` reports
  "nothing to do" and makes no API call. `list_news(ticker="SGBC")` now
  resolves through `tickers_llm`.
- `just news-tag-dry` reports the batch plan and spends nothing.
- 190 tests green (47 new: llm prompt/pricing/validation/retry, spend
  repo, tagging worker, news repo tag round-trip, config, scheduler
  wiring). Ruff clean.

**Notes / follow-ups**
- The dev sandbox this phase was built in has no `ANTHROPIC_API_KEY` and
  its proxy blocks sikafinance and api.anthropic.com, so the initial run
  used the committed fixtures and a scripted stand-in for the SDK.
  **Proven live on the Mac on 2026-08-21**: `just news-tag` tagged 50
  items across 7 batches, `pending_after=0`, cost **$0.0444** for the
  pass. Two live-only issues surfaced and were fixed in-flight:
  - The Anthropic `output_config` schema validator rejects `minimum` /
    `maximum` on integer types. Dropped from `_RESULT_SCHEMA`; the
    local `_validate` still clamps `relevance` to `[0, 10]`, so the
    invariant is preserved end-to-end.
  - The Mac's DB predated 0004; `just migrate` now applies it. Nothing
    to change in code — the on-disk schema just needed to catch up.
- The system prompt (instructions + the 69-row ticker universe) is
  ~3.7k chars / ~1.2k tokens — just over the 1024-token minimum, so the
  `cache_control` breakpoint on it should actually engage after the first
  batch of a pass. Worth confirming `usage.cache_read_input_tokens` is
  non-zero on the first real run; if it isn't, the universe table is the
  thing to grow or the breakpoint isn't worth keeping.
- `llm_spend.input_tokens` sums plain + cache-read + cache-write input
  tokens into one counter. Fine as a volume signal; the authoritative
  number for the cap is `usd_micros`, which prices each class correctly.
- Batch size 8 is a guess. If truncation (`stop_reason=max_tokens`) ever
  shows up in the logs, the fix is a smaller `LLM_BATCH_SIZE` — the code
  deliberately does not auto-split, because silently re-sending a
  half-billed batch is worse than a visible failure.
- Tests now apply **all** migrations via `tests/conftest.apply_migrations`
  instead of naming files, so migration 0005 won't need a sweep through
  the suite.
- Pre-existing mypy errors in `sources/sikafinance.py` and
  `sources/afx_kwayisi.py` (5, selectolax `Node | None` narrowing) are
  untouched — not introduced here, not in scope.
- Corporate actions are still un-tagged: they arrive already structured
  (ticker, kind, ex_date, amount) so there is nothing for the LLM to add.
  If we later ingest AGM convocations as free text, they become tagging
  input too.

---

## Phase 3c — News + corporate actions — UI (done 2026-08-21)

**Delivered**
- **Read side of `services/news.py`** — `list_feed(...)` (filters:
  ticker, category, date_from/to, min_relevance; paginated) returning
  a typed `NewsFeed`, and `list_upcoming_actions(ticker=…, days=…)`
  which joins `securities.name` for display. New view models
  `NewsRow`, `NewsFeed`, `CorporateActionRow` in `services/_view.py`;
  `Overview` gained an `upcoming_actions` field.
- **Store filters** — `store/news.list_news` / `count_news` share one
  WHERE builder (`_news_filter_clause`) so a filter change can't make
  the page count and page contents disagree. Adds category, source,
  date_from/to, min_relevance. Date bounds compare on the same
  `COALESCE(published_at, fetched_utc)` expression the ORDER BY uses,
  and `date_to='YYYY-MM-DD'` is auto-extended to end-of-day so a
  same-day filter isn't off-by-one against ISO-8601 timestamps.
- **Per-ticker `News` tab** at `/s/{ticker}/news` → uses `ticker_hint`
  OR `tickers_llm` (already wired in `store/news.list_news`). Renders
  category chip, relevance, ticker chips, EN + FR summaries when the
  Haiku tagger has stamped the row; falls back to the sikafinance
  chapeau when it hasn't. Deep-link to "Open in full news view →"
  passes the ticker through as a query filter.
- **Per-ticker `Corporate actions` tab** at
  `/s/{ticker}/corporate-actions` (equity-only) → 90-day upcoming
  table (ex-date, kind, amount + currency, yield %, pay date, note,
  source link). "TBD" rendered explicitly for `ex_date NULL`.
- **Global `/news` page** — filter form (ticker / category /
  date range / min-relevance) + HTMX pagination. Filters submit as
  `hx-get` against `/_frag/news` with `hx-swap="outerHTML"` on the
  feed div, so a change reloads only the list. Pagination replaces the
  same div with the next/prev page (25 rows per page — user's call).
  "Reset" is a plain `<a href="/news">` — no JS needed.
- **Overview 4th panel** — "Calendar · next 30d" next to
  gainers/losers/turnover on `/`. Shows up to 10 rows (date · ticker ·
  kind · amount) with a "+N more" tail; ticker cell deep-links to the
  per-ticker Corporate-actions tab. Grid switches from 3-col to 4-col
  and collapses to 2- / 1-col at narrower breakpoints.
- **Topbar** — new `News` link between Directory and Watchlists.
- **Tabs registry** — the `news` and `corporate-actions` entries no
  longer point at `_tab/placeholder.html`; the "Phase 3" marker is
  gone. `_tab/placeholder.html` still serves the Phase-4 tabs.

**Definition of Done — met**
- `just test` → 202 tests green (12 new: repo filters for
  category/relevance/date-range, service pagination + ticker-case
  handling + bogus-category tolerance, upcoming-actions join,
  News tab renders summary + ticker chips + full-view link, CA tab
  renders + hidden for indices, `/news` page filter form + narrow +
  empty state, fragment pagination round-trip, overview strip
  presence + link, topbar News link).
- Ruff clean.
- Live sanity on the Mac: `/` shows the calendar panel with SGBC /
  SPHC / TTLC / NTLC / ABJC upcoming; `/news` renders 48 ingested
  items (many un-tagged since `just news-tag` hasn't run against the
  real API yet); `/news?ticker=SNTS` narrows; `/s/SGBC/corporate-actions`
  shows the 2026-08-21 dividend row.

**Notes / follow-ups**
- **First real `just news-tag` on the Mac is still pending.** Once it
  runs, the /news feed will start showing category chips, LLM-inferred
  ticker chips, EN + FR summaries, and the min-relevance filter will
  do something visible. Until then the UI degrades to the raw
  sikafinance chapeau, which is by design.
- The News tab and the Corporate-actions tab load their data
  synchronously inside the tab route rather than as HTMX fragments —
  simpler, and the query is a couple of ms on this volume. If the news
  volume ever grows past a page or two the tab can be split into a
  lazy `hx-get` shell without touching the service layer.
- Pagination uses classic Prev/Next page replacement rather than
  "Load more" (which would need a second fragment or an append swap
  strategy). Fits the terminal aesthetic and keeps the URL bar honest.
- Filter changes on `/news` don't currently push into the browser URL
  bar — swap-only. If we want shareable filtered links, wire
  `hx-push-url="true"` on the form. Not done here because the reset
  link + the `?ticker=…` deep-link already cover the common case.
- `services/market.overview()` imports `services/news` lazily to keep
  the import graph shallow (news → sikafinance → httpx — no need to
  drag those into every market call).
- `_RELOADABLE` in `tests/conftest.py` gained `brvm.services.news`
  because `market` now imports it. Still trending toward the
  15-module refactor threshold flagged in 2.5's notes.

---

## Phase 4a — Fundamentals — filings corpus + storage (done 2026-08-21)

Ships the download + storage half of the fundamentals pipeline so 4b
can focus on extraction and UI without also having to bring up a PDF
corpus. **No UI change, no LLM call, no extraction.**

**Delivered**
- **Migration `0005_filings.sql`** — three new tables:
  - `filings` — one row per PDF: ticker (FK), issuer_name, doc_type,
    period_kind, period_year, period_label, source, source_url,
    `url_hash UNIQUE`, published_date, file_path, size_bytes, sha256,
    page_count, `is_scanned` (NULL until 4b probes), fetched_utc,
    `extracted_utc` (NULL until 4b writes). Partial index on unresolved
    `extracted_utc IS NULL` for the 4b worker.
  - `filing_source_slugs` — persisted `(source, slug) → ticker`.
    `PRIMARY KEY (source, slug)`; `ticker` may be NULL to record
    "resolver has seen this slug and cannot map it" so fuzzy matching
    isn't retried every poll. Manual `UPDATE` overrides survive
    subsequent polls (the upsert only overwrites `ticker` with a
    non-NULL incoming value).
  - `filings_spend` — separate daily counter for the 4b extractor,
    same micros-precision shape as `llm_spend` after 0004. Ready for
    the $2/day cap without a further migration.
- **`Filing` model** in `brvm.models`, with typed `FilingDocType`
  (etats_financiers · rapport_annuel · rapport_activites · resultats ·
  rse · assemblee · autre) and `FilingPeriodKind`
  (annual · H1 · Q1 · Q3 · other).
- **Config knobs** (env-configurable, defaults noted in `env.example`):
  `LLM_EXTRACT_DAILY_CAP_CENTS=200`, `EXTRACT_MAX_PDF_MB=25`,
  `FILINGS_ROOT=./data/filings`.
- **`sources/brvm_org_filings.py`** — parsers:
  - `parse_issuers_index(html)` → list of `IssuerIndexEntry(slug,
    display_name)` from `<a href="/fr/rapports-societe-cotes/…">`
    anchors. `fetch_issuers_index()` auto-paginates
    (`?page=0..N`) and stops at the first fully-seen page.
  - `parse_issuer_page(html)` → list of `ParsedFiling` — each row has
    a `<strong>ISSUER : Title</strong>` and a `<a>` PDF link. Most of
    the structured metadata (`published_date`, `doc_type`,
    `period_kind`, `period_year`) comes from the strict filename
    convention (`YYYYMMDD_-_type_-_period_-_ticker_cc.pdf`) rather
    than HTML — much sturdier against Drupal template shifts.
  - Doc-type classifier order matters (specific-first: rapport_annuel
    before rapport_activites, etats_financiers before bare-year
    fallback). `\brse\b` is *not* useful here — Python's `\b` treats
    `_` as a word character, so snake_case filenames need explicit
    lookarounds (fixed in-flight during 4a).
- **`store/spend.py` generalized** — one `SpendTable` Literal now
  reuses the same schema for both `llm_spend` and `filings_spend`
  without a second module. Existing callers unchanged (default
  `table="llm_spend"`).
- **`store/filings.py`** — `upsert_filings` (dedupe on `url_hash`),
  `exists_url_hash`, `list_by_ticker(doc_type=…)`,
  `list_needing_extraction(doc_types=("etats_financiers",
  "rapport_annuel"))` — the last is the 4b entry point.
- **`store/slugs.py`** — `get`, `get_ticker`, `remember(ticker=None)`
  (persists unresolved so we don't fuzzy-match every poll), and
  `list_unresolved(source)` for the operator report.
- **`services/filings.py`** — `resolve_ticker(source, slug,
  display_name)` builds two indexes on demand: full-name → ticker
  (matches the news resolver) plus `(root, ISO country) → ticker`.
  The second index bridges the brvm.org / sikafinance disagreement on
  country codes (brvm.org writes `BN`/`NG` where sikafinance has
  `BJ`/`NE`) and on suffixes (`BANK OF AFRICA BN` vs
  `BANK OF AFRICA BENIN`).
- **Downloader** — streams each PDF to
  `<FILINGS_ROOT>/<ticker>/<published>_<doc_type>_<period>.pdf`,
  enforces the size cap mid-stream (never keeps a partial), computes
  `sha256` while streaming, drops files < 1 KB where pypdf can't read
  a single page (almost certainly HTML masquerading as `.pdf`). One
  broken filing logs a warning and moves on — a single hiccup does
  not abort the pass.
- **`jobs/filings_pull.py` + `just filings-pull`** — one-shot demo,
  supports `MAX_ISSUERS=<n> just filings-pull` for smoke runs. Prints
  the pull counts, the 10 latest filings, and any unresolved slugs so
  the operator can hand-map them.

**Definition of Done — met**
- `just migrate` picks up `0005` idempotently.
- `just test` → 217 tests green (14 new: parsers on 2 committed
  fixtures, `_classify_period` parametrized cases, filings + slugs
  repo dedupe/upsert, slug-resolution hit/miss/persisted-NULL, country
  code bridging for BOAB/BOABF/BOAN/BOAC, `pull_all` end-to-end
  round-trip with stubbed HTTP + a tiny 2-page PDF fixture, and
  unresolved-issuer path). Ruff clean.
- Live smoke on the Mac: `MAX_ISSUERS=6 just filings-pull` walked 6
  brvm.org issuers, resolved 5 (all four BOA subsidiaries + one
  more), downloaded 100 PDFs across ~5 years to
  `data/filings/BOAN/…`, correctly refused to map `air-liquide-ci`
  (not listed in `securities`). Re-run would be a full no-op.

**Notes / follow-ups**
- Sikafinance-communiqué fallback ingestion (promoting
  `news_items[kind=communique]` rows to `filings` when the title
  matches an annual/H1 report pattern) is not landed in 4a — it was
  in the plan, but brvm.org alone covered 5/6 of the smoke sample
  and 4b can hit `filings_repo.list_needing_extraction` today. Kept
  as a small backlog item; adding it is a repo + regex change with
  no schema move.
- pypdf spams `Multiple definitions in dictionary at byte 0x…for
  key /Info` warnings on some BOA PDFs. Cosmetic; pypdf still returns
  the right page count.
- `air-liquide-ci` and any other brvm.org issuer not in `securities`
  are recorded as unresolved slugs. The auto-discovery machinery for
  new equity listings runs on the sikafinance AAZ page (see
  `sources/sikafinance.parse_aaz`); an issuer that's on brvm.org but
  not on AAZ probably means it's a bond/warrant/OPCVM. Bond
  ingestion is on the backlog.
- `pull_all` currently walks every issuer serially with a 0.5 s
  sleep between requests. On the full 75-issuer universe that's
  ~40 s just for pacing, plus HTTP time. Perfectly fine for daily
  scheduled runs; if it needs to be a foreground command more often
  we can parallelize per-issuer.

---

## Phase 4b — Fundamentals — extraction + Financials/Ownership/Segments (done 2026-08-22)

Consumes the 4a corpus to fill the three placeholder tabs on `/s/{ticker}`.

**Delivered**
- **Migration `0006_fundamentals.sql`** — three tables, all keyed on
  `(ticker, period_year, period_kind)` so a re-extraction of the same
  period overwrites cleanly:
  - `financials` — P&L core (revenue, operating_income, net_income,
    EPS, dividend/share) + balance-sheet (total_assets, total_equity)
    + currency. `filing_id` FK for the audit trail.
  - `financial_segments` — business / geographic revenue split,
    `share_pct` in `[0, 100]`.
  - `ownership` — top shareholders, `share_pct` + optional absolute
    share count when the report gives one.
- **`services/extraction.py`** — pypdf → text (empty output ⇒
  `is_scanned=1`, skip forever), a chars/4 pre-flight cost estimate so
  a filing that would breach `filings_spend` is deferred before we
  spend, Haiku call using the same shape as 3b's `services/llm.py`
  (structured `json_schema` output + local validation, one corrective
  retry, `LLMResponseError` carries the usage billed so far). Text sent
  to the model is capped at 120k chars ≈ 30k input tokens so a huge
  RSE annex can't blow one filing past the cap on its own.
- **`services/fundamentals.py`** — the worker (`extract_pending`) plus
  the read helpers the UI consumes (`get_financials_series` /
  `get_segments` / `get_ownership`). Same two invariants as the
  news-tagger keep it from becoming a money pit:
  1. **Never re-process a filing.** Every filing handed to a
     successful (or parse-failed) call gets `filings.extracted_utc`
     stamped. Only a pre-flight refusal (budget exhausted, empty text,
     file missing) leaves the row alone for a future pass.
  2. **Hard $2/day cap.** Checked against `filings_spend` before every
     call; the real cost is committed straight after so a crash
     mid-pass can't lose spend.
- **`store/financials.py`** — `replace_period` atomically clears the
  triple then re-inserts, deduping repeated segments/holders inside a
  single call so the model's occasional "Autres" repeat doesn't
  violate the composite PK.
- **`store/filings.mark_extracted`** — the stamper the worker calls
  after every extraction attempt, with optional `is_scanned` update.
- **UI** — the three placeholder tabs now render real views:
  - **Financials** — 6-year annual table, currency labelled, empty
    rows collapsed. XOF-native but respects the extractor's per-row
    currency for issuers with EUR/USD comparatives.
  - **Ownership** — top holders + % (+ absolute share count when known)
    for the latest extracted period.
  - **Segments** — side-by-side business + geographic split for the
    latest period. Only the buckets the extractor found are rendered.
- **`jobs/fundamentals_extract.py`** + `just fundamentals-extract` /
  `just fundamentals-extract-dry` — one-shot demo. Scheduler adds
  `fundamentals_extract_daily` (03:00 Abidjan, well after close +
  before the sector job).
- **Corpus insight from the dry-run.** Against the BOA sub-corpus (5
  tickers, ~100 filings) the pre-flight reports ~17/20 of the recent
  filings as scanned — French annual reports lean heavily on
  image-only PDFs. `is_scanned=1` short-circuits those without an
  LLM call, so the effective spend surface is much smaller than the
  filings count suggests. OCR is on the backlog.

**Definition of Done — met**
- `just migrate` picks up `0006` idempotently; the three new tables
  land on a fresh DB.
- `just fundamentals-extract-dry` reports pending/scanned/would-extract
  counts and spends nothing.
- `just test` → 248 tests green (31 new: extraction preflight / prompt
  / retry / clamps, financials repo replace + overwrite + read helpers,
  worker end-to-end via a scripted `FakeAnthropic` covering happy path
  + budget cap + scanned + missing file + failed call + empty payload +
  no-key + dry-run read-only + read helpers, scheduler wiring, page
  render for both empty state and populated state). Ruff clean.

**Notes / follow-ups**
- **First real `just fundamentals-extract` on the Mac is still pending.**
  Once run, spend lands in `filings_spend` (separate from `llm_spend`)
  and the Financials / Ownership / Segments tabs on `/s/{ticker}`
  populate for whichever issuers had a text-extractable annual report.
- Char/4 heuristic for the pre-flight matches Anthropic's public rule
  of thumb; it slightly over-estimates for filings full of numeric
  tables (digits tokenize dense) which is the safe direction for a
  budget gate. If actual bills consistently underrun the estimate we
  can loosen the divisor.
- Batch size is deliberately 1: an annual report is 30-50k input tokens
  on its own, and mixing filings would risk cross-report contamination
  in the model's output. Prompt caching still amortizes the system
  prompt across the day's pass.
- The **retry-on-parse** billing is exactly the tagger's model: both
  attempts count against the daily cap, and the filing is stamped so
  a permanently-broken extract doesn't cost 2× on every subsequent
  pass. The tests pin both invariants.
- `_RELOADABLE` in `tests/conftest.py` gained `extraction` +
  `fundamentals` (14 modules; the 15-module refactor threshold from
  Phase 2.5's notes is one phase away).
- Interim (H1/Q1/Q3) extraction stays on the backlog — a straight
  repeat of 4b's path once we've proven the annual pipeline against
  real Anthropic bills.

---

## Phase 4c — Fundamentals — OCR + interim extraction + sikafinance-communiqué fallback (done 2026-08-23)

Closes the three coverage holes 4b's live pass surfaced:

* Most 2025/2026 annual reports are image-only PDFs, so `is_scanned=1`
  short-circuited them before Haiku got a look.
* Interim reports (`rapport_activites`, ~60 filings in the current corpus)
  were sitting in `filings` but excluded by 4b's doc-type gate.
* brvm.org occasionally misses a filing that sikafinance publishes as a
  `communiqué`. Nothing was promoting those to the corpus.

**Delivered**

- **Migration `0007_ocr.sql`** — adds `filings.ocr_attempted_utc TEXT` +
  a partial index `ix_filings_pending_ocr` on
  `(is_scanned=1 AND ocr_attempted_utc IS NULL)` so the nightly OCR
  query touches only unresolved rows.
- **`services/ocr.py`** — thin wrapper around the `ocrmypdf` CLI (via
  `subprocess.run`, so a per-file timeout cleanly kills stray tesseract
  processes). Injectable `runner` callable keeps the pipeline testable
  without a real tesseract install. Handles the four operational cases:
  success (rewrite the file, refresh sha256 + size + page_count), failure
  (`nonzero_exit`, `timeout`, `no_output` → stamp attempt, leave
  `is_scanned=1`), `already_ocr` (return code 6 — treat as success
  because the file secretly had a text layer), and `OcrUnavailable`
  (binary missing → abort the whole pass without stamping the untouched
  tail so a rerun after `brew install` picks up where we left off).
- **`store/filings.py`** — three new helpers keeping the OCR bookkeeping
  in one place: `list_pending_ocr` (partial-index-backed),
  `count_pending_ocr`, `apply_ocr_success` (flips `is_scanned=0` and
  clears `extracted_utc` so the row re-enters `list_needing_extraction`
  on the next pass), `apply_ocr_failure` (stamp only).
- **`jobs/filings_ocr.py`** + `just filings-ocr` — one-shot demo. Prints
  the same shape as `fundamentals-extract` (pending_before / considered
  / ok / already_had_text / failed / missing_file / pending_after) with
  a clear "install ocrmypdf" hint if the binary is missing.
- **Scheduler** — `filings_ocr_daily` at 02:00 Abidjan, one hour ahead
  of `fundamentals_extract_daily` so newly-OCR'd filings land in the
  same night's extraction pass.
- **Interim extraction wiring.** `list_needing_extraction` default
  doc_types now include `rapport_activites` alongside `etats_financiers`
  and `rapport_annuel` — that alone unlocks ~60 filings in the current
  BOA sub-corpus. The system prompt gained explicit period-kind
  guidance ("interim reports report period-to-date figures, not
  annualised — return those as-is"; leave EPS/dividend_per_share null
  for interims that don't state them; don't reach for the shareholder
  register on activity reports). Storage was already keyed on
  `(ticker, period_year, period_kind)` so no migration needed.
- **Financials tab** — annual table stays as-is; a compact "Latest
  interim" card renders below it when `get_latest_interim(ticker)`
  finds an H1/Q1/Q3 row newer than the latest annual. Explicitly
  labelled "period-to-date, not annualised" to keep casual readers
  from mistaking it for a full-year figure. The helper hides the card
  when the annual is already at least as new — mixing a stale H1 with
  a fresh full-year would mislead more than inform.
- **Sikafinance-communiqué fallback.** New in `services/filings.py`:
  `classify_communique_title` (pure function, ordered patterns to keep
  "rapport annuel" beating "rapport d'activités") and
  `promote_from_communiques(client=None)`. Reads
  `news_items[kind='communique', source='sikafinance']` via a LEFT JOIN
  on `filings.source_url` so an already-promoted URL never re-enters
  the loop, resolves the issuer through the same `(name, ISO country)`
  index used by the brvm.org resolver, does a
  `(ticker, doc_type, period_kind, period_year)` cross-source dedupe
  against brvm.org before downloading, and stores files under
  `data/filings/<TICKER>/sikafinance_<stem>.pdf` so a filename can't
  collide with the brvm.org twin.
- **`just filings-pull`** now runs the promotion step at the tail of
  each pass; `--skip-promote` opts out for a plain brvm.org-only pull.

**Two invariants the tests pin (OCR)**

1. **Never re-OCR automatically.** Every filing handed to the runner
   gets `ocr_attempted_utc` stamped, whether the pass ended in success,
   `nonzero_exit`, `timeout`, or a missing-on-disk file. `OcrUnavailable`
   is the one exception: nothing is stamped so a re-run after installing
   the binary starts exactly where we stopped.
2. **Success re-queues extraction.** `apply_ocr_success` clears
   `extracted_utc` in the same UPDATE that flips `is_scanned=0`, so
   the row falls back into `list_needing_extraction` on the next pass
   — no cross-module signalling needed.

**Definition of Done — met**

- `just migrate` picks up `0007` idempotently.
- `just test` → 292 tests green (44 new: OCR service ok/fail/timeout/
  already-ocr/missing-binary/missing-file/page-cap, OCR repo helpers,
  extraction default gate expansion, `get_latest_interim` behaviour,
  sikafinance title classifier over 10 filing-worthy titles and
  9 non-filings, promote end-to-end with a stubbed downloader,
  cross-source dedupe, network-failure handling). Ruff clean.
- Live demo shape unchanged: `just filings-pull` now emits a second
  block ("filings promote (sikafinance)"); `just filings-ocr` runs the
  nightly OCR sweep; `just fundamentals-extract` picks up interim +
  newly-OCR'd rows automatically.

**Notes / follow-ups**

- **OCR toolchain is optional.** No Python-side dep on `ocrmypdf` (the
  package pulls in a compiled tesseract at install time, which we don't
  want to force on CI). The service shells out via `subprocess`; if the
  binary isn't on PATH the pass sets `unavailable=1` and returns, and the
  rest of the app is unaffected. This matches CLAUDE.md's
  "graceful degradation" charter.
- **OCR wall-clock budgeting.** `OCR_MAX_FILES_PER_RUN=20` and
  `OCR_TIMEOUT_S=600` are conservative defaults — a 20×10-min slot is
  roughly the 02:00→03:00 gap in the scheduler. If the real corpus turns
  out to run faster, raise both; if a couple of pathological scans
  routinely time out, lower `OCR_MAX_PAGES` (currently 400) to skip
  them entirely.
- **First live `just filings-ocr` still pending.** Once run, the ~21
  scanned filings in the current BOA sub-corpus should get text layers,
  re-enter the extractor queue, and populate the Financials tab for the
  years where 4b's dry-run reported "scanned".
- **Interim card is annual-first.** The card intentionally hides when the
  annual for a given year is already extracted — a Q1 2025 sitting next
  to a full-year 2025 is more distraction than signal.
- **Sikafinance dedupe is by exact triple.** If sikafinance and brvm.org
  disagree on `period_kind` for the same underlying filing (e.g. one
  calls a 6-month report "2eme trimestre" and the other "1er
  semestre"), the promoter would let both in. `classify_communique_title`
  folds `Q2 → H1` on the sikafinance side to reduce that surface, but
  the possibility remains.
- `_RELOADABLE` in `tests/conftest.py` now covers `services.filings`
  and `services.ocr` (16 modules). The refactor-to-injected-settings
  threshold flagged in 2.5's notes is now crossed; keeping it on the
  list explicitly for the next phase.

---

## Phase 4d — Fundamentals — financial ratios (done 2026-08-24)

Turns the P&L + balance-sheet rows shipped in 4b/4c into ratios the UI
and (in Phase 6) the analyst-note prompt can reason about. No LLM calls,
no schema for the ratios themselves — computed on demand from the
existing `financials` table, the latest `quote_snapshots.last`, and a
small `securities` extension for `shares_outstanding` / `float_pct` /
`market_cap_xof`.

**Delivered**

- **Migration `0008_company_facts.sql`** — extends `securities` with
  `shares_outstanding`, `float_pct`, `market_cap_xof`, and
  `company_facts_refreshed_utc`. Kept on `securities` rather than a
  new `company_facts` table because the relationship is strictly 1:1
  per ticker and the ratios engine reads all four values via one
  `SELECT`.
- **`services/ratios.py`** — pure ratio math split by category:
  * **Valuation** — P/E, P/B, P/S, dividend yield, payout ratio,
    earnings yield. Price-based ratios flip to `None` when
    `financials.currency != quote_snapshots` price currency (XOF by
    convention here; EUR/USD comparatives from a few issuers otherwise
    would produce a bogus multiple).
  * **Profitability** — ROE, ROA, net margin, operating margin.
  * **Growth (annual only)** — revenue / net-income / EPS YoY,
    computed against the *immediately-prior period of the same kind*.
    A Q1→annual comparison returns None rather than mixing
    period-to-date with full-year.
  * **Leverage** — financial leverage (`total_assets / total_equity`),
    equity ratio.
  Each ratio is wrapped in a `Ratio(value, provenance, unit)` dataclass
  so the template can show a "how this was computed" tooltip and the
  Phase 6 analyst prompt can quote the arithmetic back to the reader.
- **`services/company_facts.py` + `just company-refresh`** — one-shot
  refresh of the sikafinance societe page for every stale equity.
  Parses `"100 000 000"` → int, `"22,47%"` → float, `"3 440 000 MFCFA"`
  → 3.44 trillion XOF. Idempotent within `max_age_days` (default 7);
  stamps `company_facts_refreshed_utc` even on `no_data` rows so a
  totally-empty issuer isn't refetched every week.
- **Scheduler** — `company_facts_refresh_weekly` (Sun 04:30 Abidjan,
  right after the sector job) so the ratios engine's inputs stay
  fresh without manual intervention.
- **Financials tab** — new Ratios table under the annual financials
  (one row per ratio, one column per period, dense — rows with no
  values across any period are hidden). Plus a compact interim-ratio
  block under the "Latest interim" card: net margin, operating margin,
  ROE only, because valuation and YoY growth on a period-to-date figure
  would mislead more than inform.
- **Peers tab** — three new columns (P/E, ROE, NET MARG) sourced from
  `services.ratios.get_latest_ratios(peer.ticker)`. Missing values
  render as "—" so a peer that hasn't been through fundamentals
  extraction still lays out correctly. Titles on the `<th>` explain
  each metric.
- **`services/company.get_peers_with_ratios`** — the annotator the
  pages route calls; keeps the peers cache untouched (60-min TTL on
  sector membership) but computes ratios fresh on every render
  because prices tick intraday.

**Two invariants the tests pin**

1. **Missing / zero divisor → None, never inf or NaN.**
   `test_zero_divisor_never_produces_inf_or_nan` asserts this across
   the whole ratio surface — the Financials tab must render "—" and
   move on, not crash.
2. **Growth ratios need same-kind prior.** An H1 next to an annual
   prior returns None — mixing period-to-date and full-year figures
   is worse than no signal.
3. **Currency mismatch suppresses price ratios.** If a filing was
   extracted with `currency='EUR'` but the price snapshot is in XOF,
   P/E / P/B / P/S / earnings yield all come back as None with a
   `currency_mismatch=True` flag the template surfaces to the user.

**Definition of Done — met**

- `just migrate` picks up `0008` idempotently on the live DB.
- `just test` → **328 tests green** (36 new: ratio math edge cases,
  currency-mismatch handling, growth-ratio same-kind gate, DB-facing
  helpers over a seeded DB, `parse_shares` / `parse_float_pct` /
  `parse_market_cap_xof` across nbsp + French-formatted inputs,
  refresh idempotence, HTTP-failure survival, peers annotation
  populating `pe`/`roe`/`net_margin` on peers with financials and
  leaving them None on peers without). Ruff clean.
- Live: SNTS's Financials tab renders the Ratios table alongside the
  annual + interim tables; SNTS's Peers tab shows ORAC / ONTBF with
  P/E / ROE / NET MARG when those peers have extracted financials.

**Notes / follow-ups**

- **P/FCF, FCF yield, EV/EBITDA** are deferred. They need a
  `cash_flow_ops` + `capex` + derived `free_cash_flow` column on
  `financials`, plus a re-run of the extractor over the ~200 already-
  processed filings (call cost roughly comparable to the initial 4b
  pass; well under the $2/day cap). Flagged in the backlog above.
- **Currency-mismatch handling is coarse.** We treat every price as
  XOF (which is true for BRVM equities) and only look at the
  `financials.currency` column. An issuer that reports its cover in
  XOF but drops EUR EPS figures inside the annexes would still flow
  through the pipeline as `currency='XOF'` — the mismatch flag only
  helps for the case where the extractor already normalised on the
  reported currency.
- **The Peers annotation is one SQL query per peer.** At ~5 peers per
  sector this is fine; if the peers table ever gets much larger, the
  annotation would want a single-query join through
  `financials` + `quote_snapshots` + `securities` — kept as a
  follow-up.
- **Growth ratios walk the returned series in memory** rather than a
  self-join. Simpler to reason about, and the series is at most 6
  rows deep (5-year table + one buffer).
- **`_RELOADABLE` in `tests/conftest.py` gains** `ratios` and
  `company_facts` (18 modules). The "refactor toward injected
  settings" flag from 2.5's notes is now overdue; carrying it into
  the next phase.
- **First real `just company-refresh` still pending** — expected to
  populate `shares_outstanding` for the ~47 active equities, at which
  point every P/E, P/B, P/S on the Financials tab lights up.

---

## Phase 5 — TUI (Textual) — not started

Planned scope: Watchlist + quotes + news ticker sharing the same service
layer as the web app.

---

## Phase 6a — Alerts (done 2026-08-24)

First slice of Phase 6: a rule engine over the data we already collect
(snapshots, filings, tagged news) plus a delivery worker that pushes
matches out over Discord.

**Also folded in**: the `_RELOADABLE` refactor that Phase 2.5 flagged.
`brvm/config.py`'s `settings` is now a lazy proxy — attribute access
loads a cached `Settings()` on first use, and `reset_settings_cache()`
drops the cache. The 17 test files that previously did `importlib.reload`
sweeps now just monkeypatch envs + call the reset. Fewer test-isolation
sharp edges (see below).

**Delivered**

- **Migration `0009_alerts.sql`** — two tables:
  - `alert_rules` (kind + optional ticker + kind-specific fields:
    `threshold_pct`, `min_relevance`, `doc_types`) with a partial index
    on enabled rows.
  - `alert_events` (rule_id FK, kind, ticker, subject, body,
    `payload_json`, `dedupe_key`, `fired_utc`, `delivered_utc`,
    `delivery_status`) with `UNIQUE(rule_id, dedupe_key)` and a partial
    index on `delivered_utc IS NULL` (the delivery queue).
- **Models** — `AlertRule` and `AlertEvent` pydantic models with
  `AlertKind = Literal["price_move", "new_filing", "news"]` and
  `AlertDeliveryStatus = Literal["ok", "failed", "skipped"]`.
- **`store/alerts.py`** — rule CRUD (`create_rule`, `list_rules`,
  `get_rule`, `set_enabled`, `delete_rule`) and event helpers
  (`record_event` returns None on dedupe hit, `list_undelivered`,
  `mark_delivered`, `list_recent`, `count_undelivered`).
- **`services/alerts.py`**:
  - `evaluate_price_moves(conn, rules)` — dedupe by
    `snap:<ticker>:<captured_utc>`, watchlist-wide with `ticker=None`.
    `quote_snapshots` has no synthetic id (PK is composite), so the
    ticker + timestamp is the natural key.
  - `evaluate_new_filings(conn, rules, since_utc=None)` — dedupe by
    `filing:<id>`, doc_types CSV narrows the match.
  - `evaluate_news(conn, rules)` — dedupe by `news:<id>`, only reads
    rows with `relevance IS NOT NULL` (untagged rows have nothing to
    gate on), matches ticker via `ticker_hint` OR the `tickers_llm` CSV.
  - `evaluate_all()` — single entry point the scheduler calls.
  - `deliver_pending(sender=None, limit=None)` — walks the queue,
    stops on first failure (a webhook that's failing shouldn't be
    spammed with every queued event on the same pass), marks delivered
    rows `ok`, leaves failed rows queued with `delivery_status='failed'`
    for the next pass. No webhook configured → all events marked
    `skipped` so the queue doesn't grow forever on a fresh install.
- **Discord sender** — thin wrapper around `httpx.Client.post` with the
  webhook URL. Payload is `{content, username}` (plain markdown, no
  embeds — terminal aesthetic).
- **Jobs + scheduler**:
  - `jobs/alerts_evaluate.py` + `just alerts-eval` — prints the
    counts + last 10 events.
  - `jobs/alerts_deliver.py` + `just alerts-deliver` — drains queue.
  - `alerts_evaluate_market_hours` (mon-fri 09-14 Abidjan, `11-59/15`
    — offset +11 min from news poll so tagger has had time to run),
    `alerts_evaluate_hourly_outside` (:41), `alerts_deliver_every_5min`
    (`*/5`).
- **UI**:
  - `/alerts` page with a rules table (kind chip, ticker, trigger
    summary, label, on/off toggle, delete button) and a recent-events
    feed (color-coded by delivery status). "no webhook" badge when
    `DISCORD_WEBHOOK_URL` is unset.
  - Add-a-rule form with kind-specific field groups toggled by inline
    `hx-on:change` so a `price_move` picker only shows
    `threshold_pct`, etc.
  - HTMX endpoints under `/_frag/alerts/rules` for POST create, POST
    `/toggle`, DELETE.
  - Topbar `Alerts` link.
- **Settings** — `DISCORD_WEBHOOK_URL` (empty ⇒ disabled),
  `ALERTS_DEDUPE_WINDOW_HOURS=24`, `ALERTS_DELIVERY_BATCH=10`,
  `settings.has_discord` convenience property.
- **`config.py` refactor** — `settings` is now a `_SettingsProxy` that
  lazy-loads a cached `Settings()` on attribute access, plus
  `reset_settings_cache()` for tests. `tests/conftest.py` lost
  `_RELOADABLE` + `_reload_all`; the `client` fixture calls a small
  `reset_module_state()` that resets the cache + clears the two TTL
  caches (company / history) + drops the memoized Anthropic client.

**Two invariants the tests pin**

1. **Never re-fire a rule for the same event.** Every evaluator computes
   a natural `dedupe_key` (snapshot's `ticker:captured_utc`, filing id,
   news id) and the `UNIQUE(rule_id, dedupe_key)` at the store layer
   drops any repeat.
2. **Never lose an event.** `delivered_utc IS NULL` is the entire
   queue definition; a webhook 5xx or a missing `DISCORD_WEBHOOK_URL`
   leaves rows for a later pass (or explicitly stamps them `skipped` so
   they don't accumulate forever with no notifications). The batch cap
   keeps recovery from becoming a flood.

**Definition of Done — met**

- `just migrate` picks up `0009` idempotently.
- `just alerts-eval` runs against the live DB and reports counts +
  recent events. Verified live: 24 events fired for a wildcard
  |Δ|≥1% rule against the current snapshots, second run reported 24
  deduped.
- `just alerts-deliver` with no webhook: 10 events marked `skipped`,
  reason `no_webhook`.
- `/alerts` renders empty state; POSTing a rule adds it; toggle flips
  enabled; DELETE removes it. Topbar carries the link.
- `just test` → **379 tests green** (42 new: 19 in
  `test_alerts_service.py` covering the store, all three evaluators,
  dedupe, and delivery in every operational shape; 10 in
  `test_alerts_ui.py` covering the page + form + toggle + delete + 404
  branches; 3 lines added to `test_scheduler_windows.py`). Ruff clean.
  The one pre-existing failure
  (`test_poll_all_ingests_and_dedupes` — SGBC fixture date drift) is
  unchanged.

**Notes / follow-ups**

- **Settings proxy exposes a pre-existing test-isolation leak in
  `test_market_service.py`.** The old suite let `_reload_all()` +
  monkeypatch ordering hide the fact that the test only applied
  migrations 0001+0002 despite calling `market.overview()` (which
  reaches into `corporate_actions` from 0003). Fixed by pointing the
  `_seed()` helper at `conftest.apply_migrations` — the same helper
  every other post-3a test already uses.
- **No AlertRule ↔ AlertEvent audit view yet.** The events feed is
  sorted by fire time; if a rule fires the same "kind of thing"
  (e.g. daily price moves on SNTS) frequently the feed will be
  dominated by that rule. Grouping/filtering is a follow-up if it
  becomes noisy in practice.
- **Delivery is Discord-only.** The interface is small enough
  (`_DiscordSender.send(event) -> (bool, str)`) that a second sink
  (Slack, email) would slot in without a schema move — but nothing
  needs it today.
- **Dedupe window setting is currently informational.**
  `ALERTS_DEDUPE_WINDOW_HOURS` lands in `.env` but the store dedupe
  is per-event-identity rather than per-time-window. If a rule ever
  needs "fire once per 24h regardless of underlying event," we'd
  extend `dedupe_key` to include a coarsened timestamp. Left for
  when a real-world false-positive shows up.
- **First real `just alerts-deliver` on the Mac with a Discord webhook
  is still pending.** Once configured, the same rule set that lit up
  24 events on the smoke test will start pushing to the channel.

---

## Phase 6b — Daily brief (done 2026-08-25)

Post-close markdown brief synthesized by Haiku from the day's indices,
top movers, high-relevance tagged news, and next-7-day corporate
actions. One row per UTC day (rerun overwrites — there's only one brief
for a given day). Cost accounting is fully isolated in `brief_spend`,
independent of the 3b tagger and 4b extractor budgets.

**Model choice**: started on Haiku, not Sonnet 4.6 as first sketched.
At ~2k output tokens per brief the price step doesn't justify the
prose-quality lift on a very structured, JSON-fed prompt. `BRIEF_MODEL`
in `.env` flips to Sonnet if the write-ups feel thin once real days
have run.

**Delivered**

- **Migration `0010_briefs.sql`** — two tables:
  - `briefs` (day PK, model, title, markdown, `context_json`,
    input/output tokens, `usd_micros`, `generated_utc`, `session_date`).
    `context_json` stores the raw snapshot the model saw so a future
    re-run with a different prompt doesn't need to re-gather.
  - `brief_spend` — same shape as `llm_spend` after 0004; `SpendTable`
    literal in `store/spend.py` now covers all three counters.
- **`Brief` model** — day + model + markdown + context_json +
  token/cost accounting.
- **`store/briefs.py`** — `upsert` (INSERT OR REPLACE, overwrite same
  day in one statement so the UI never renders a half-written brief),
  `get`, `latest`, `list_recent`, `count`.
- **`services/brief.py`**:
  - `gather_context(day)` — pulls `market.overview` + `news_svc.list_feed`
    (filtered to `day` and `min_relevance`) + `news_svc.list_upcoming_actions`.
    Same read paths as the UI so the brief can't diverge from what a
    human sees at ~15:00.
  - `_SYSTEM_PROMPT` — structured markdown output (`# Session recap` →
    `# Movers` → `# News that matters` → `# Watch tomorrow`), grounded
    on the JSON snapshot with an explicit "never invent figures" rule.
    English body from French source per the charter.
  - `generate_for(day, client=None, dry_run=False)` — one-shot end-to-
    end. Pre-checks `brief_spend` against `BRIEF_DAILY_CAP_CENTS` (50)
    before the call, records real usage against the same day the brief
    covers (not `utcnow()`) so a `--date` override rolls up cleanly,
    stamps `title` from the first `# heading` line.
- **`services/llm.py` public helpers** — `response_text()` and
  `usage_from_response()` promoted from `_` names (Phase 6c will reuse
  them for analyst-note synthesis).
- **`jobs/brief_run.py`** + `just brief-run` / `just brief-run-dry` /
  `just brief-run --date YYYY-MM-DD` (via `python -m brvm.jobs.brief_run`).
  Scheduler: `brief_daily` at 15:30 Africa/Abidjan Mon-Fri (BRVM closes
  ~15:00; the news tagger has finished stamping relevance by then).
- **`/brief`** page — latest brief with a "machine-generated" badge, an
  archive sidebar (last 30 days, current one highlighted).
  **`/brief/YYYY-MM-DD`** for archive links. Server-side render via
  `markdown-it-py` with `html=False` so raw HTML in the markdown source
  is escaped (asserted by a test).
- **Topbar** — new `Brief` link between `Alerts` and the search box.
- **CSS** — dark, dense, monospace: styled markdown headings match the
  accent color, sidebar sits right of the article on desktop and
  stacks below on narrow screens.
- **Settings** — `BRIEF_MODEL` (Haiku 4.5 dated snapshot),
  `BRIEF_DAILY_CAP_CENTS=50`, `BRIEF_MIN_RELEVANCE=6`,
  `BRIEF_MAX_NEWS_ITEMS=30`, `BRIEF_MAX_OUTPUT_TOKENS=2048`,
  `settings.brief_daily_cap_micros` convenience property.
- **`markdown-it-py`** added to `pyproject.toml` deps. Pure Python, no
  compiled extensions — installs cleanly on the Mac and the VPS.

**Two invariants the tests pin**

1. **Overwrite same day.** `test_upsert_overwrites_same_day` +
   `test_generate_overwrites_same_day` — running twice on the same day
   leaves exactly one row, and it's the newer content.
2. **Never lose spend accounting.** `test_generate_empty_reply_bills_but_marks_failed`
   asserts that a model reply we can't use (empty content) still bills
   the input tokens into `brief_spend`; the budget cap can't be
   bypassed by a failed brief. A transport error before the API sees
   any tokens is the opposite case — `test_generate_transport_error_is_reported_not_billed`
   pins that at zero.

**Definition of Done — met**

- `just migrate` picks up `0010` idempotently.
- `just brief-run-dry` prints gather-only counts and spends nothing.
  Live smoke on the Mac: `2026-08-25: 0 news, 20 movers, 8 upcoming
  actions`; with `--date 2026-08-20`, `1 news`. Correctly falls back
  when a day has no tagged news.
- `just brief-run` end-to-end (verified with a scripted `FakeAnthropic`;
  live Haiku call still pending — see below).
- `just test` → **400 tests green** (21 new: 3 store + 2 gather + 8
  service including happy-path, overwrite, dry-run, no-key, budget
  exhausted, empty-reply, transport error, read helpers; 8 UI covering
  empty state, latest render, `/brief/{day}` archive, 404 branches,
  HTML escaping; 1 scheduler-wiring line). One pre-existing failure
  (`test_poll_all_ingests_and_dedupes` — SGBC fixture date drift) is
  unchanged.
- Ruff clean.

**Notes / follow-ups**

- **First live `just brief-run` on the Mac with a real key still
  pending.** Once it runs, `brief_spend` starts accumulating; expect
  fractions of a cent per weekday at Haiku rates. If output quality
  reads thin, flip `BRIEF_MODEL=claude-sonnet-4-6` and re-run — the
  pricing table in `services/llm.py` already covers Sonnet.
- **Test-isolation fix folded in.** The `client` fixture now also
  monkeypatches `DISCORD_WEBHOOK_URL=""` + `ANTHROPIC_API_KEY=""` so a
  developer's `.env` doesn't leak into rendered HTML (the `/alerts`
  "no webhook" badge test regressed on my box after Phase 6a because
  I'd set the webhook URL for a smoke test). Pinning both to empty in
  the fixture keeps tests independent of local secrets.
- **`response_text` + `usage_from_response` are now public** — 6c's
  analyst-note synthesis will use them verbatim, so I stopped
  reaching for the underscore-prefixed names from another module.
  The old `_` aliases are kept as one-line back-compat handles.
- **Archive is unpaginated at 30 rows.** Enough for the first quarter
  of daily briefs. If we ever want deeper history, add
  `/brief?offset=…` — the store already has a `limit`-based query.
- **`context_json` isn't shown in the UI.** It's stored purely for
  reproducibility; a future "regenerate with this prompt tweak"
  workflow can read it back without re-hitting sikafinance. If it
  needs a viewer, a `/brief/{day}/context` endpoint would be a
  small addition.

---

## Phase 6c — Analyst-note synthesis — not started

Planned scope: since BRVM has essentially no public sell-side coverage,
generate our own per-ticker note weekly by feeding the LLM the recent
news + financials + ratios + price action. Rendered on the
`Analyst view` tab of `/s/{ticker}` (added in this phase). Clearly
labelled as machine-generated.
