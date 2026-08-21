# brvm-terminal

A lightweight, terminal-aesthetic dashboard for the **BRVM** (Bourse
Régionale des Valeurs Mobilières — the regional stock exchange for the 8
WAEMU countries, based in Abidjan). Single-user, low-memory, reliability
over features. See [CLAUDE.md](./CLAUDE.md) for the full project charter.

## Status

See [`docs/phases.md`](./docs/phases.md) for the running log.

- [x] Phase 0 — scaffold
- [x] Phase 1 — reference data + quotes
- [x] Phase 2 — web UI v1
- [x] Phase 2.5 — search + directory + company tab shell
- [x] Phase 3a — news + corporate actions (ingest)
- [x] Phase 3b — news + corporate actions (Haiku tagging, $1/day cap)
- [ ] Phase 3c — news + corporate actions (UI: `/news`, tabs, 30-day strip)
- [ ] Phase 4 — fundamentals (financials, ownership, segments)
- [ ] Phase 5 — TUI
- [ ] Phase 6 — alerts + daily brief + analyst-note synthesis

## Requirements

- macOS or Linux
- [uv](https://docs.astral.sh/uv/) (dependency manager)
- [just](https://github.com/casey/just) (task runner)
- Python 3.12

On macOS: `brew install uv just python@3.12`.

## Setup

```bash
cp env.example .env        # edit if you have any keys; defaults work
just sync                  # create .venv, install deps
just migrate               # create data/brvm.sqlite with initial schema
just test                  # offline fixture-based tests
just dev                   # http://127.0.0.1:8765
```

`ANTHROPIC_API_KEY` in `.env` is the only key that changes behaviour
today — it turns on the Phase 3b news tagger. Leave it blank and
everything else still works; news is simply stored untagged.

## Try it (Phase 2)

After `just migrate` + `just snapshot`, run the web app and open the
terminal in your browser:

```bash
just snapshot   # populate the DB with the latest quotes
just dev        # http://127.0.0.1:8765
```

Available pages:

- `/` — market overview (indices strip + gainers / losers / turnover leaders,
  auto-refresh every 60s during market hours, 5 min otherwise)
- `/directory` — full securities table with country / sector / kind /
  text filters (HTMX)
- Topbar **search** — type ticker or name; Enter jumps to the first hit
- `/s/{TICKER}` — single security page with tabs: Overview (Lightweight
  Charts price history) · Description (profile + shareholders) · Peers
  (sector peer table). News / Corporate actions / Financials / Ownership /
  Segments tabs are stubs that fill in Phases 3 & 4.
- `/watchlists` — create and manage named watchlists
- `/watchlists/{slug}` — quote board for one list, add/remove tickers inline
- `/health` — JSON liveness

## Try it (Phase 3a demo)

After `just migrate`, run one news+communiqués+dividends poll:

```bash
just news-poll
```

Prints the row-count summary (news / communiqués inserted vs deduped,
dividend-calendar rows inserted vs updated), the 5 latest news items,
and the next-30-day corporate-actions calendar. Second run against the
same fixtures reports 0 new rows — dedupe on `url_hash` for news, and
`(ticker, kind, ex_date)` pre-check for corporate actions.

The web UI still shows the Phase 2.5 shell tabs ("Coming in Phase 3");
the news/actions tabs light up in Phase 3c.

## Try it (Phase 3b demo — news tagging)

Tags every news item ingested by `just news-poll` with Claude Haiku:
tickers, relevance 0-10, category, and a 1-2 sentence summary in both
French and English.

```bash
cp env.example .env         # then set ANTHROPIC_API_KEY=sk-ant-...
just news-poll              # ingest first (Phase 3a)
just news-tag-dry           # see the batch plan; spends nothing
just news-tag               # tag for real
```

Sample output:

```
news tagging:
   pending_before = 40
          batches = 5
           tagged = 40
       unanswered = 0
   failed_batches = 0
   skipped_budget = 0
    pending_after = 0
    cost this run = $0.0295
      spend today = $0.0295 / $1.0000 cap

llm_spend 2026-08-21: calls=5 in=7000 out=4500 ($0.0295)
```

(Batch counts are from a real 40-item pass over the committed fixtures;
the token/cost figures are indicative — actual usage depends on how much
of the ~1.2k-token system prefix comes back as a cache read.)

What it guarantees:

- **Hard $1/day cap.** Real per-call cost is written to `llm_spend` in
  micro-dollars right after every call, and the budget is re-checked
  before each batch. Once the day is spent the worker no-ops with a
  warning until UTC midnight. Change the ceiling with
  `LLM_DAILY_CAP_CENTS`.
- **Never re-processed.** Every item handed to a successful call gets
  `tagged_utc` stamped, so re-running `just news-tag` costs nothing.
- **Degrades quietly.** No `ANTHROPIC_API_KEY`, an exhausted budget, or a
  failing API all end in counts + a log line, never a crash — the
  scheduled job is safe to leave on.

Tagging also runs on the scheduler (7 minutes behind each news poll:
`*/15` during market hours, hourly otherwise), so `just dev` keeps the
feed tagged on its own.

The tagged fields (`tickers_llm`, `relevance`, `category_llm`,
`summary_fr`, `summary_en`) are what Phase 3c's `/news` page and the
per-ticker News tab will render.

## Try it (Phase 1 demo)

After `just migrate`, run one live snapshot cycle and print the top-10
securities by daily turnover:

```bash
just snapshot
```

Example output:

```
TICKER   NAME                                     LAST     CHG%       VOLUME     TURNOVER XOF
---------------------------------------------------------------------------------------------
SPHC     SAPH CI                              8,990.00   +7.02%       52,971      476,209,290
BICB     BANQUE INTERNATIONALE POUR LE CO     8,295.00   -2.35%       52,947      439,195,365
SGBC     SGBCI                               39,200.00   -0.25%        7,088      277,849,600
...
```


## Data sources

Phase 1 goes scraper-first — the "BRVM Market Data API" referenced in
`CLAUDE.md` was withdrawn in June 2026. Sources actually used:

- **sikafinance.com** — canonical A-to-Z listing, per-ticker cotation and
  historique, palmarès. French number formatting (space thousands, comma
  decimal).
- **afx.kwayisi.org/brvm** — cross-check + last-10-day OHLCV per ticker.
- **brvm.org** — daily Bulletin Officiel de la Cote PDF, sector quotes.

The `BRVM_API_*` env vars are still recognised so a future paid feed
(EODHD, ICE) can be dropped in behind `services/providers.py` without
touching the service layer.

The news intelligence layer calls the Anthropic API with
`claude-haiku-4-5-20251001` (override with `ANTHROPIC_MODEL`). It is the
only outbound non-scraping call the app makes.

## Layout

```
src/brvm/
  sources/    # fetchers + pure parsers, one module per source
  store/      # thin SQLite repositories (WAL mode)
  services/   # business logic; only layer the UI touches
  jobs/       # APScheduler tasks (market-hours aware, Africa/Abidjan)
  apps/web/   # FastAPI + Jinja2 (dark terminal aesthetic)
```

## Testing

All tests run offline: scrapers against committed HTML/PDF fixtures in
`tests/fixtures/`, and the tagging pipeline against a fake Anthropic
client (`tests/_fake_anthropic.py`) — `just test` never spends a cent or
touches the network. Refresh the fixtures (dev-only, hits the network)
with:

```bash
just refresh-fixtures
```
