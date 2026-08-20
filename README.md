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
- [ ] Phase 3b — news + corporate actions (Haiku tagging, $1/day cap)
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

All scraper tests run offline against committed HTML/PDF fixtures in
`tests/fixtures/`. Refresh them (dev-only, hits the network) with:

```bash
just refresh-fixtures
```
