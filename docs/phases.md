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
| 3b | News + corporate actions — Haiku tagging ($1/day cap) | not started | — |
| 3c | News + corporate actions — UI (news feed, per-ticker tabs, 30-day strip on /) | not started | — |
| 4 | Fundamentals (financials, ownership, segments) | not started | — |
| 5 | TUI (Textual) | not started | — |
| 6 | Alerts + daily brief + analyst-note synthesis | not started | — |

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

## Phase 3b — News + corporate actions — Haiku tagging — not started

Planned scope:
- `services/llm.py` thin Anthropic client (`claude-haiku-4-5-20251001`
  per charter) with strict JSON schema output and retry-on-parse-failure.
- Daily spend cap enforced against `llm_spend`: **hard limit $1/day**;
  worker no-ops with a warning once the day's `usd_cents` crosses 100.
- Backfill script + incremental worker (only `news_items` with
  `tagged_utc IS NULL`); writes `tickers_llm` CSV, `relevance` (0-10),
  `category_llm`, `summary_fr`, `summary_en`.
- Batch prompts where the API allows to keep per-item cost minimal.

## Phase 3c — News + corporate actions — UI — not started

Planned scope:
- `/news` page: filter by ticker + category + date, HTMX pagination.
- Fill `News` tab on `/s/{ticker}` (per-ticker feed) and `Corporate
  actions` tab (upcoming dividends / AGMs for that ticker).
- **Next-30-days corporate-actions strip on `/`** (small, dense —
  right rail or a fourth panel next to gainers/losers/turnover).
- No dedicated `/calendar` global page (per user; per-ticker + overview
  strip is enough).

---

## Phase 4 — Fundamentals (financials, ownership, segments) — not started

New phase covering the PDF-driven parts of the Bloomberg-style company
page. These are absent from public BRVM data as structured feeds — only
available as annual-report / états-financiers PDFs, mostly in French.

Planned scope:
- **PDF corpus**: crawl sikafinance communiqués + brvm.org filings for
  documents tagged "états financiers" / "rapport annuel". Store PDFs
  under `data/filings/<ticker>/` with a `filings` table for metadata.
- **Extraction pipeline**: pypdf for structural parsing, Haiku for
  structured extraction — schema like `{period, currency, revenue,
  operating_income, net_income, total_assets, total_equity,
  segments: [{name, revenue, share}], geo: [{country, revenue}],
  ownership: [{holder, pct}]}`.
- New tables `financials`, `financial_segments`, `ownership`.
- Fills the `Financials`, `Ownership`, `Segments / Revenue breakdown`
  tabs on `/s/{ticker}`.
- Best-effort: coverage varies wildly by company; UI must render
  gracefully when a section is missing.

Explicit non-goals: sell-side analyst estimates (moved to Phase 6),
intraday tick data (out of scope for the product).

---

## Phase 5 — TUI (Textual) — not started

Planned scope: Watchlist + quotes + news ticker sharing the same service
layer as the web app.

---

## Phase 6 — Alerts + daily brief + analyst-note synthesis — not started

Planned scope:
- Price-move + new-filing alerts (local `alerts` table + optional
  Discord webhook).
- Post-close daily brief job — stronger model than the Phase 3 tagger,
  outputs a short markdown brief of movers + tagged news.
- **Analyst-note synthesis**: since BRVM has essentially no public
  sell-side coverage, generate our own per-ticker note weekly by feeding
  the LLM the recent news + financials + price action. Rendered on the
  `Analyst view` tab of `/s/{ticker}` (added in this phase). Clearly
  labelled as machine-generated.
