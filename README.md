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
- [x] Phase 3c — news + corporate actions (UI: `/news`, tabs, 30-day strip)
- [x] Phase 4a — fundamentals (filings corpus + storage)
- [x] Phase 4b — fundamentals (Haiku extraction + Financials/Ownership/Segments tabs)
- [x] Phase 4c — fundamentals (OCR + interim extraction + sikafinance-communiqué fallback)
- [x] Phase 4d — fundamentals (financial ratios on the Financials + Peers tabs)
- [ ] Phase 5 — TUI
- [ ] Phase 6 — alerts + daily brief + analyst-note synthesis

## Requirements

- macOS or Linux
- [uv](https://docs.astral.sh/uv/) (dependency manager)
- [just](https://github.com/casey/just) (task runner)
- Python 3.12
- **Optional (Phase 4c OCR):** `ocrmypdf` + tesseract with the French
  language pack. Without it, `just filings-ocr` no-ops with a warning and
  scanned filings stay unextractable — everything else works.

On macOS: `brew install uv just python@3.12`.
For OCR: `brew install ocrmypdf tesseract-lang`.

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
- `/s/{TICKER}` — single security page with tabs: Chart (Lightweight
  Charts price history) · Description · Peers · News · Corporate actions ·
  Financials · Ownership · Segments. Tabs with no data yet render a
  graceful empty state.
- `/news` — filterable news feed (ticker / category / date / min-relevance)
  with HTMX pagination
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
`summary_fr`, `summary_en`) power the `/news` page and the per-ticker
News tab that Phase 3c wired up.

## Try it (Phase 4a / 4b demo — filings + fundamentals extraction)

Phase 4a pulls annual/interim PDFs from `brvm.org` into `data/filings/`
and records one row per PDF in `filings`; Phase 4b extracts structured
fundamentals from those PDFs with Haiku and fills the Financials /
Ownership / Segments tabs on `/s/{TICKER}`.

```bash
MAX_ISSUERS=6 just filings-pull       # walk 6 issuers, download PDFs
just fundamentals-extract-dry         # see the plan + estimated cost
just fundamentals-extract             # extract for real ($2/day cap)
just dev                              # /s/BOAC/financials etc.
```

`just fundamentals-extract-dry` is read-only — it probes each PDF with
pypdf, reports which are scanned (skipped by 4b — real OCR is on the
backlog) and how much a full pass would cost, without spending a cent or
mutating the DB. `just fundamentals-extract` writes to the fundamentals
tables and to `filings_spend` (its own daily counter, separate from
`llm_spend` — an annual report is orders of magnitude bigger than a news
batch, so extraction has its own $2/day ceiling via
`LLM_EXTRACT_DAILY_CAP_CENTS`).

What it guarantees:

- **Hard $2/day cap.** Same shape as 3b: real cost accounted in
  `filings_spend` micros right after every call, budget re-checked
  before every filing, worker no-ops with a warning until UTC midnight
  once crossed.
- **Never re-processed.** Every filing handed to a call (successful,
  failed, or empty) gets `filings.extracted_utc` stamped so a re-run
  costs nothing. Scanned PDFs also get `is_scanned=1` so pypdf never
  probes them again.
- **Degrades quietly.** No `ANTHROPIC_API_KEY`, an exhausted budget, a
  missing PDF on disk, or a failing API all end in counts + a log line,
  never a crash.

Extraction also runs daily on the scheduler at 03:00 Africa/Abidjan
(`fundamentals_extract_daily`), well after market close.

## Try it (Phase 4c demo — OCR + sikafinance fallback + interim)

Phase 4c fills the gaps 4b left open:

- **OCR** rescues scanned French annual reports so the extractor can pick
  them up. Requires the `ocrmypdf` binary (see Requirements above).
- **Sikafinance-communiqué fallback** promotes filing-worthy communiqué
  rows (états financiers / rapport d'activités) into the `filings`
  corpus, catching reports brvm.org missed. Runs automatically at the
  tail of `just filings-pull`.
- **Interim extraction** extends the extractor's default gate to include
  `rapport_activites`, and the Financials tab now shows the most recent
  H1/Q1/Q3 as a separate card above the annual table (period-to-date
  figures don't belong in a year-over-year row).

```bash
just filings-pull            # brvm.org walk + sikafinance promotion
just filings-ocr             # OCR every is_scanned=1 filing (free, CPU-only)
just fundamentals-extract    # extract, including newly-OCR'd + interim
```

Guarantees:

- **Never re-OCR automatically.** Every filing handed to the OCR runner —
  success or failure — gets `filings.ocr_attempted_utc` stamped. An
  operator forcing a retry clears that column manually.
- **Cross-source dedupe.** The sikafinance promoter checks the
  `(ticker, doc_type, period_kind, period_year)` triple before
  downloading, so the same H1 report from both brvm.org and sikafinance
  is stored once.
- **Bounded per-file OCR time.** `OCR_TIMEOUT_S=600` (per file) and
  `OCR_MAX_FILES_PER_RUN=20` keep the nightly slot honest.

OCR runs daily on the scheduler at 02:00 Africa/Abidjan
(`filings_ocr_daily`), one hour ahead of the extractor so newly-text-
layered filings land in the same night's cycle.

## Try it (Phase 4d demo — financial ratios)

Phase 4d turns the extracted `financials` rows into ratios (P/E, P/B,
P/S, dividend yield, payout, ROE, ROA, margins, YoY growth, financial
leverage, equity ratio) and renders them on:

- **`/s/{TICKER}/financials`** — a Ratios table under the annual
  financials, plus a small interim-ratios block (net margin, operating
  margin, ROE) under the interim card.
- **`/s/{TICKER}/peers`** — new P/E / ROE / net-margin columns for
  cross-ticker comparison in the same sector.

Ratios need `securities.shares_outstanding` (fetched from
sikafinance). Refresh it weekly:

```bash
just company-refresh   # walk stale rows, hit sikafinance societe pages
just dev               # /s/SNTS/financials → Ratios block + Peers with P/E
```

The runner is polite (0.5s between requests) and idempotent within a
week — a rerun within `OCR_MAX_AGE_DAYS` (default 7) is a no-op.
Runs automatically on the scheduler every Sunday at 04:30 Africa/Abidjan
(`company_facts_refresh_weekly`).

**Deferred**: P/FCF, FCF yield, and EV/EBITDA are on the backlog — they
need cash-flow (`cash_flow_ops`, `capex`) added to the extractor first.
See `docs/phases.md` for the Phase 4d writeup.

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
