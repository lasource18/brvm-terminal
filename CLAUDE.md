# BRVM Terminal

A lightweight "Bloomberg terminal" for securities listed on the BRVM
(Bourse Régionale des Valeurs Mobilières, Abidjan — regional exchange for
the 8 WAEMU countries). Runs on an 8GB MacBook and a small Hetzner VPS
(CX22, 4GB RAM). Single user. Optimize for low memory, low complexity,
and reliability over feature count.

## Market facts (important for correctness)
- ~47 listed equities + WAEMU sovereign/corporate bonds. Currency: XOF (CFA franc, pegged to EUR at 655.957).
- Trading hours: Mon–Fri, roughly 09:00–15:00 GMT (Africa/Abidjan, UTC+0). Continuous trading with a single daily fixing legacy for some securities — verify current session structure at brvm.org.
- All primary sources are in FRENCH. Numbers use space as thousands separator and comma as decimal (e.g. "1 301 997 599" / "481,64"). Parse accordingly.
- Key indices: BRVM Composite (BRVMC), BRVM 30, BRVM Prestige, sector indices.
- Tickers are short codes (e.g. SNTS = Sonatel, ORAC = Orange CI, BOAC = Bank of Africa CI, ETIT = Ecobank ETI, ONTBF = Onatel BF, TTLC = Total CI, SGBC = SGB CI, PALC = PalmCI). Build the canonical ticker table from a live source, do NOT hardcode from memory.

## Data sources (priority order)
1. BRVM Market Data API — commercial API with delayed quotes (15 min), indices, 25y daily OHLCV, company reference data, and structured BRVM announcements. Credentials in .env as BRVM_API_KEY / BRVM_API_BASE. Treat as primary when configured; degrade gracefully when not.
2. brvm.org — official site: daily Bulletin Officiel de la Cote (PDF), communiqués, company filings. Scrape politely.
3. sikafinance.com — news + official company communiqués (états financiers, AG, dividendes) + quotes pages (e.g. /marches/cotation_<TICKER>, /marches/communiques_brvm, /marches/historiques/<TICKER>). No public RSS: poll HTML. This is the main NEWS source.
4. afx.kwayisi.org/brvm — clean daily summaries, gainers/losers. Good cross-check.
5. richbourse.com — fundamentals/dividend history cross-check.

Scraper etiquette: identify with a custom User-Agent, cache aggressively, poll at most every 10–15 min during market hours and hourly otherwise, exponential backoff, never parallel-hammer a domain. Every scraper must have fixture-based tests (saved HTML samples in tests/fixtures/) so site redesigns are caught by tests, not runtime crashes.

## News intelligence layer
- Anthropic API key in .env as ANTHROPIC_API_KEY. Use claude-haiku-4-5-20251001 for high-volume tagging, a stronger model only for the daily brief.
- Pipeline: fetch article/communiqué → dedupe (hash URL+title) → Haiku call returns strict JSON: {tickers: [], relevance: 0-10, category: earnings|dividend|governance|macro|capital_action|other, summary_fr: 1-2 sentences, summary_en: 1-2 sentences} → store.
- Daily brief job after market close: summarize movers + tagged news into a short markdown brief.
- Budget-conscious: batch where possible, never re-process an article, cap daily API spend via a simple counter.

## Stack (do not deviate without asking)
- Python 3.12, uv for dependency management
- FastAPI + Jinja2 + HTMX for the web UI (server-rendered, terminal aesthetic: dark, dense, monospace)
- Textual for the TUI (shares the same service layer — UI is a thin shell)
- SQLite via sqlite3/SQLAlchemy core (no heavy ORM), WAL mode
- APScheduler for jobs (market-hours aware, Africa/Abidjan tz)
- httpx + selectolax (BeautifulSoup fallback) for scraping; NO Playwright unless a source is proven JS-only — ask first
- Charts: TradingView Lightweight Charts via CDN on the web side; textual-plotext or sparklines in TUI
- Deployment: single docker-compose (app + caddy) OR bare systemd service; target < 500MB RSS total

## Architecture rules
- Strict layering: sources/ (fetchers+parsers, one module per source) → store/ (SQLite repos) → services/ (quotes, news, corporate actions, briefs) → apps/web and apps/tui consume services only.
- Every fetcher returns typed dataclasses/pydantic models; parsers are pure functions (HTML in → models out) for testability.
- All timestamps stored UTC; render in Africa/Abidjan or America/Montreal per user setting.
- Config via .env + pydantic-settings. Never commit secrets.
- Graceful degradation: if a source is down, the UI shows stale-data badges with the last-updated time, never crashes.

## Conventions
- Ruff for lint/format, pytest for tests, type hints everywhere.
- Conventional commits. Small, reviewable commits per feature.
- French strings from sources stay in French in the DB; translate only at the LLM/summary layer.

## Definition of done per phase
Code runs, tests pass (pytest -q), ruff clean, README updated with any new setup step, and a demo command or URL I can try immediately.
