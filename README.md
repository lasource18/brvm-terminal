# kodji-terminal

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
- [x] Phase 5 — TUI (Textual, parity with the web)
- [x] Phase 6a — alerts (price move + new filing + news relevance)
- [x] Phase 6b — daily brief (post-close, Haiku)
- [x] Phase 6c — analyst-note synthesis (weekly per-ticker, Sonnet)
- [x] Phase 7 — cash-flow extraction (P/FCF, FCF yield, EV/EBITDA) + filings-link references on the Financials tab
- [x] PR-X — accounts, users and per-account ownership
- [x] PR-X2 — magic-link sign-in (Resend) + session cookies

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
just migrate               # create data/kodji.sqlite with initial schema
just test                  # offline fixture-based tests
just dev                   # http://127.0.0.1:8765
```

`ANTHROPIC_API_KEY` in `.env` is the only key that changes behaviour
today — it turns on the Phase 3b news tagger. Leave it blank and
everything else still works; news is simply stored untagged.

### Schema drift

Both entry points refuse to start when `migrations/` is ahead of the DB
recorded in `_schema_migrations`:

```
kodji.db.PendingMigrations: 1 migration(s) not applied to ./data/kodji.sqlite:
0020_example. Run `just migrate` before starting the app.
```

This is deliberate. Before the check, a deploy that shipped a migration
but never ran `just migrate` booted clean and then returned a 500 on the
first request that touched the new column — at whatever hour a user
happened to open that page. Now the mistake surfaces at the moment it is
made, when the fix is one command.

To ask without starting anything — for a deploy script, ahead of the
service restart:

```bash
just migrate-check     # exit 0 = up to date, 1 = pending (names them)
```

A DB that is *ahead* of the files (rolled back to older code) is not
drift the app can fix by migrating, so it does not block startup.

### Upgrading from `brvm-terminal`

The project was renamed to `kodji-terminal`. Your `.env` is not tracked by
git, so the rename cannot reach it — update it by hand on every machine
(including the VPS) before starting the app:

```bash
# .env
DB_PATH=./data/kodji.sqlite                              # was ./data/brvm.sqlite
HTTP_USER_AGENT=kodji-terminal/0.1 (+contact: you@example.com)
```

Then move the database to match:

```bash
sqlite3 data/brvm.sqlite 'PRAGMA wal_checkpoint(TRUNCATE);'
mv data/brvm.sqlite data/kodji.sqlite
rm -f data/brvm.sqlite-shm data/brvm.sqlite-wal
```

Do not skip this. SQLite **creates an empty database** rather than failing
when `DB_PATH` points at a file that no longer exists, so a stale `.env`
gives you a silently empty app — HTTP 200s with no securities — instead of
a startup error. The schema itself is unaffected: `_schema_migrations`
tracks migration ids only, so no re-migration is needed.

The console script is now `kodji-tui` (was `brvm-tui`), and the installed
package is `kodji` (was `brvm`); re-run `just sync` to refresh both.

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
- `/health` — JSON liveness, plus the scheduler's verdict under `jobs`
  (see *Ops — the job watchdog* below)

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

**Follow-up (shipped in Phase 7)**: P/FCF, FCF yield, and EV/EBITDA now
render alongside the earlier ratios — see the Phase 7 Try-it section
below. `docs/phases.md` has the full writeup for both phases.

## Try it (Phase 6a demo — alerts)

Phase 6a adds a rule engine over the existing snapshots / filings /
tagged news, and pushes matched events to Discord (optional).

```bash
just dev                    # /alerts — create + toggle + delete rules
just alerts-eval            # one eval pass — fires matching events
just alerts-deliver         # drain queue via DISCORD_WEBHOOK_URL
```

Rule kinds:

- **`price_move`** — fires when `|change_pct| ≥ threshold_pct` on the
  latest snapshot. `ticker=None` scans every security (watchlist-wide).
- **`new_filing`** — fires on each new row in `filings`. Narrow by
  `ticker` and/or a CSV of `doc_types`.
- **`news`** — fires on Haiku-tagged news whose `relevance ≥
  min_relevance` and whose attribution (`ticker_hint` or `tickers_llm`
  CSV) matches the rule's ticker. Untagged rows don't participate.

Guarantees:

- **Never re-fire.** `(rule_id, dedupe_key)` is UNIQUE at the store
  layer — a re-eval on the same snapshot / filing / news row is a no-op.
- **Never lose an event.** `delivered_utc IS NULL` is the queue; a
  webhook outage leaves rows for the next pass. Batch cap
  (`ALERTS_DELIVERY_BATCH=10`) keeps recovery from becoming a flood.
- **Degrades quietly.** No `DISCORD_WEBHOOK_URL` → events are marked
  `skipped` and stay visible on `/alerts` for manual review.

Alerts also run on the scheduler: eval every 15 min during market hours
(offset +11 from the news poll so tagged relevance has settled), hourly
otherwise; delivery every 5 min.

## Try it (Phase 6b demo — daily brief)

Post-close markdown brief synthesized by Haiku from the day's indices,
top movers, high-relevance tagged news, and next-7-day corporate
actions. Overwrites the same-day row on rerun (there's only one brief
for a given day).

```bash
just brief-run-dry            # gather-only: prints context counts
just brief-run                # real call ($0.50/day cap)
just dev                      # /brief (latest) + /brief/YYYY-MM-DD
```

The scheduler wires `brief_daily` at 15:30 Africa/Abidjan Mon-Fri —
BRVM closes ~15:00, and the news tagger has run by then so relevance
scores are settled. `/brief` renders the latest brief server-side via
`markdown-it-py`; the sidebar lists the last 30 days for archive
browsing.

Guarantees:

- **Hard $0.50/day cap** in `brief_spend` (a separate counter from
  `llm_spend` and `filings_spend`). One brief per weekday at Haiku
  rates rounds to fractions of a cent; the cap is a safety net.
- **Overwrite on rerun**, not append — the store `INSERT OR REPLACE`s
  the row for `day`, so a mid-day dry-run followed by the real
  post-close run leaves the good one.
- **Degrades quietly.** No `ANTHROPIC_API_KEY`, an exhausted cap,
  an empty reply, or a transport error all end in counts + a log
  line, never a crash. `context_json` is stored so a future re-run
  with a different prompt doesn't need to re-gather the source data.

The brief is **clearly labelled machine-generated** in the UI so
readers don't mistake the synthesis for editorial commentary.

## Try it (Phase 5 demo — Textual TUI)

The TUI reads the same SQLite as the web app and calls the same
services layer. A dense terminal shell with a persistent watchlist
sidebar and a right pane that swaps between screens.

```bash
just tui              # or: uv run python -m kodji.apps.tui
# also available as `kodji-tui` on the PATH after `just sync`
```

Layout:

```
┌ header ─────────────────────────────────────────────────────────┐
│ ● OPEN   last snapshot: 42s ago             2026-08-26 09:42 Abidjan │
├────────────────────────┬────────────────────────────────────────┤
│ Watchlist / Turnover   │  Home / Ticker / Directory / News /    │
│   leaders (◂/▸ arrows) │  Watchlists / Alerts (h/t/d/F5/w/a)    │
│   Enter → open ticker  │                                        │
├────────────────────────┴────────────────────────────────────────┤
│ footer: keybinding hints                                        │
└─────────────────────────────────────────────────────────────────┘
```

Keybindings:

| Key       | Action                                            |
|-----------|---------------------------------------------------|
| `h`       | Home (indices strip + movers + high-relevance news) |
| `t`       | Ticker view (last selected)                       |
| `d`       | Directory (sortable columns: 1W / 1M / 3M / YTD / 1Y / ALL) |
| `F5`      | News feed (`/` inside for filters)                |
| `w`       | Watchlists (create / delete / add / remove)       |
| `a`       | Alerts (events inbox + rules editor)              |
| `ctrl+k`  | Command palette — search ticker or company name   |
| `shift+w` | Cycle sidebar watchlist                           |
| `r`       | Force refresh now                                 |
| `q`       | Quit                                              |

Refresh model: `set_interval(30)` during market hours (paused
off-hours via `clock.is_market_open()`); repaints preserve
`DataTable.cursor_coordinate` and scroll offset so the timer
doesn't yank the cursor around. Manual `r` always works.

The ticker view mirrors the web `/s/{ticker}` tabs (Overview, Chart,
News, Financials, Peers, Corp actions, Brief, Analyst view). Charts
use `plotext` for an inline Braille line-plot. Markdown (brief +
analyst note) renders via Textual's `Markdown` widget.

## Try it (Phase 6c demo — analyst notes)

Weekly per-ticker synthesis on the new `Analyst view` tab of
`/s/{ticker}`. Sonnet reads the last 30 days of tagged news, 5-year
annual financials + latest interim, computed ratios, 90-day price
stats, ownership + segments — then writes a ~1000-word markdown note
grouped as: Snapshot / Recent developments / Financial position /
Ratios read-across / Risks & watch items.

```bash
just analyst-notes-run-dry --ticker SNTS   # gather-only: context counts
just analyst-notes-run --ticker SNTS       # one ticker, real Sonnet call
just analyst-notes-run --limit 5           # smoke-run 5 tickers
just analyst-notes-run                     # full weekly pass ($3/day cap)
just dev                                   # /s/SNTS/analyst
```

The scheduler wires `analyst_notes_weekly` at 20:00 Africa/Abidjan on
Saturday — after Friday's close, all the weekend enrichment jobs
(sector, company-facts, history backfill are set for Sunday but the
note doesn't need them). By Monday's open the archive lists the newly-
written notes on every equity page. Archive sidebar links to
`/s/{TICKER}/analyst/YYYY-MM-DD` for prior weeks.

Guarantees:

- **Hard $3/day cap** in `note_spend` (its own counter, separate from
  `llm_spend` / `filings_spend` / `brief_spend`). A full 47-ticker
  pass at Sonnet rates ≈ $1.90; the cap gives one full retry of
  headroom, and `NOTES_DAILY_CAP_CENTS` gates a rerun that would
  drain the budget.
- **Overwrite by week, not append.** The store keys on
  `(ticker, week_start)` and `INSERT OR REPLACE`s — a rerun mid-week
  produces a fresher take on the same week's data, which is what a
  reader expects.
- **Only active equities.** Indices and bonds are skipped; the tab
  404s for indices.
- **Degrades quietly.** No `ANTHROPIC_API_KEY`, an exhausted cap, an
  empty reply, or a transport error all end in counts + a log line,
  never a crash. `context_json` is stored so a future re-run with a
  different prompt doesn't need to re-gather the source data.

The note is **clearly labelled machine-generated** in the UI so
readers don't mistake the synthesis for sell-side research. There is
no sell-side research on the BRVM — that's the whole point.

## Try it (Phase 7 demo — cash-flow + filings references)

Three new cash-flow columns on `financials` — `cash_flow_ops`, `capex`,
`free_cash_flow` — populated by the Haiku extractor from the
"Flux de trésorerie" section of each annual report. The Financials tab
gains a P/FCF, FCF yield, and EV/EBITDA proxy in the ratios table, and
a **References** section that lists the source filings behind each
row with a link back to the original PDF.

```bash
just fundamentals-recover-cashflow-dry   # count filings needing re-extract
just fundamentals-recover-cashflow       # clear extracted_utc on those
just fundamentals-extract                # re-run against the reset filings
just dev                                 # /s/SNTS/financials
```

The recovery job is idempotent — once a row has any of the three
cash-flow columns populated, it's out of the query. Extraction still
respects the `LLM_EXTRACT_DAILY_CAP_CENTS=200` daily cap; a full ~200-
filing backfill fits comfortably in one day at Haiku rates.

Notes on the multiples:

- **P/FCF** and **FCF yield** use market cap (`shares * price`) and are
  suppressed on currency mismatch or when FCF ≤ 0 (yield still shown as
  a signed % — the direction matters).
- **EV/EBITDA** is a **proxy** — we don't yet ingest net debt or D&A,
  so EV = market cap and EBITDA ≈ operating income (RBE). The Ratios
  cell hovers a `title` with the exact formula, and the table footer
  spells out the caveat so nobody screens on it as a textbook multiple.
- **References** section lists every `(period, doc_type)` currently
  backing the persisted rows, joined onto `filings` for the audit
  trail. Annual filings are surfaced above interims inside a given
  year (that's usually what the reader came for).

## Try it (Phase 8 demo — bond ingestion)

Bonds finally join the securities table. brvm.org publishes three
category pages (state / regional / private); `just bonds-poll` walks
all three and upserts the rows.

```bash
just migrate
just bonds-poll        # first run: securities=~100 bars=~100
just bonds-poll        # re-run:    all UPSERTs, no growth
just dev               # /directory?kind=bond
```

`kind=bond` rows land in `securities` (with `sector` set to the French
category label — `Obligations d'Etat` / `Obligations régionales` /
`Obligations privées`), and today's price lands in `daily_bars.close`
so the same period-return SQL that powers equities and indices covers
bonds too. Period returns will read mostly 0% — bonds anchor to par
(10 000 XOF) and only drift on rare secondary-market trades.

State bond issuer country is derived from `ETAT DU {country}` in the
name (mapping covers all eight WAEMU members). Regional and private
bonds stay `country=NULL` — they aren't tied to a single country and
we prefer honest nulls over guesses.

## Try it (PR-X2 demo — magic-link sign-in)

No password anywhere. You submit an email address, we mail a link **and**
a 6-digit code, and either one signs you in.

With no `RESEND_API_KEY` set, the mailer logs the message instead of
sending it — so you can complete a real sign-in locally without signing
up for anything:

```bash
just migrate
just dev                       # http://127.0.0.1:8765/login
```

Submit your address, then read the link (or the code) off the terminal:

```
WARNING kodji.services.mailer: email not sent (no RESEND_API_KEY) — to=you@example.ci subject=Votre lien de connexion Kodji
Bonjour,

Voici votre lien de connexion à Kodji Terminal :

    http://127.0.0.1:8765/login/t/lS3k...

Ou saisissez ce code dans l'onglet où vous avez demandé la connexion :

    418207
```

Paste the link, or type the code into the form that's already on screen.
The topbar then shows your address and a **Sign out** button, and the
watchlists and alert rules you create belong to your account and nobody
else's.

### Sending real email

Resend is the provider (chosen 31 Aug 2026 — see
[`docs/kodji-plan.md`](./docs/kodji-plan.md)). Two settings turn it on:

```bash
# .env
RESEND_API_KEY=re_...
EMAIL_FROM=Kodji <connexion@mail.kodji.app>
EMAIL_REPLY_TO=support@kodji.app      # optional; see below
PUBLIC_BASE_URL=https://kodji.app     # required in production, see below
```

`EMAIL_REPLY_TO` matters more than it looks. The sender is on
`mail.kodji.app`, which has no mailbox behind it (only Resend's bounce
handler), so a user who hits Reply on the sign-in mail — "I never got
the code" is a common one — bounces. Point it at a PrivateEmail alias
on the apex that you actually read.

Three things matter more than the vendor choice:

- **Authenticate a sending subdomain**, not the apex: SPF, DKIM and
  DMARC on `mail.kodji.app`. Gmail and Yahoo have required alignment
  from bulk senders since 2024, and a good chunk of BRVM's audience is
  on one or the other. The apex belongs to the human mailbox
  (PrivateEmail) — keeping the two apart means an app-side spam
  complaint cannot touch your own mail.
- **Keep the daily brief off this sender.** A brief blast is bulk-shaped
  and attracts complaints; sign-in mail must not share its reputation.
- **Set `PUBLIC_BASE_URL` in production.** Behind Cloudflare and Caddy
  the request's own host is whatever the last proxy claimed, and a link
  built from a spoofed `Host` header is a live credential pointed at
  someone else's domain.

Note the value is unquoted in `.env`: `EMAIL_FROM=Kodji <connexion@...>`.
If you do quote it, use straight ASCII quotes on both ends — a smart
quote from a text editor becomes part of the address and Resend rejects
every message with a 422.

DNS, when the domain already hosts a mailbox (kodji.app on
PrivateEmail):

| Host | Type | Why |
| --- | --- | --- |
| `mail.kodji.app` | TXT (DKIM) + MX + SPF, all from Resend's dashboard | The sending subdomain. Resend's MX is the bounce return path; it does not touch apex mail. |
| `kodji.app` | MX → PrivateEmail, TXT SPF → `include:spf.privateemail.com` | Unchanged. This is where you *receive*. |
| `_dmarc.kodji.app` | TXT `v=DMARC1; p=none; rua=mailto:you@kodji.app` | Start at `p=none`, read the reports for a week, then tighten to `quarantine`. It covers subdomains too. |

If you move the nameservers to Cloudflare, copy **every** PrivateEmail
record across before the switch — MX, apex SPF, DKIM, the autodiscover
CNAMEs — and leave all of them DNS-only (grey cloud). Proxying an MX
host silently breaks mail delivery.

### Abuse caps on the sign-in form

Three, in the order they are checked:

| Cap | Setting | On trip |
| --- | --- | --- |
| Per address, per hour | `LOGIN_MAX_PER_HOUR=5` | Same "check your email" page, nothing sent — a stranger learns nothing about who has been asking for links. |
| Global, per hour / per day | `LOGIN_MAX_SENDS_PER_HOUR=30` · `LOGIN_MAX_SENDS_PER_DAY=80` | `503` with `Retry-After`, an honest "temporarily unavailable", and an `ERROR` log line. |
| Wrong-code guesses per challenge | `LOGIN_CODE_MAX_ATTEMPTS=5` | The challenge is burned; ask for a new link. |

The global one is the spray defence: a script posting 100 *different*
addresses passes the per-address cap every time and would otherwise
spend Resend's free-tier quota (100/day) in a minute — locking every
real user out until the reset and making `mail.kodji.app` a source of
unwanted mail. Keep the daily cap under the provider's quota.

**Per-IP is deliberately not done in the app.** Behind Caddy the app
sees `127.0.0.1` for every request, and trusting a forwarded header is
a deploy-time decision. Once the site is behind Cloudflare, add a
Rate Limiting rule (the free plan includes one, with the period and
block fixed at 10 seconds): *if* `URI Path equals /login` *and*
`Request Method equals POST`, *then* block above 3 requests per
10 seconds per IP. That throttles a burst; the global cap underneath
it is what actually bounds the damage. Exact steps are in the
[deploy runbook](./docs/deploy-kodji-app.md).

### Turning sign-in from optional into required

`AUTH_REQUIRED` is `false` today, which keeps the existing single-user
box working exactly as it does: a request with no session resolves to
the account migration 0017 seeded. **Set it to `true` before the app is
reachable by anyone but you** — with it off, an anonymous visitor reads
that account's data.

**First, claim that account.** Nothing links an email address to the
seeded account 1, so your own first sign-in would mint a fresh free
account and none of your watchlists or alert rules would be in it:

```bash
just claim-owner you@example.com     # idempotent; then sign out and in
```

Plan gating (PR-Y) does *not* depend on that flag: an anonymous request
resolves to the default account and is enforced against whatever plan it
holds. The two are independent — gating decides *what* a caller sees,
`AUTH_REQUIRED` decides *whether* a caller has to identify themselves.

## Try it (PR-Y demo — plan gating)

Raw market facts are free; what the app computes on top of them is paid.
The split is `docs/kodji-plan.md` P4, and `/pricing` renders it.

Your own account stays on paid — migration 0019 puts account 1 there, so
gating can't lock the operator out of their own terminal. To *see* the
free tier, flip it and flip it back:

```bash
just migrate                 # applies 0019
uv run python - <<'EOF'
from kodji.db import connect
from kodji.config import settings
from kodji.store import accounts as repo
with connect(settings.db_path) as c:
    repo.set_plan(c, 1, "free")
EOF
just dev
# /              → renders; Alerts and Brief drop off the topbar
# /s/SNTS/chart  → 402 with an upgrade wall
# /api/history/SNTS → 402 {"error": "payment_required"}
# /pricing       → free vs paid, always reachable
# adding an 11th distinct ticker to a watchlist → 402 + a cap notice
```

Put yourself back with `repo.set_plan(c, 1, "paid")`.

Where the enforcement lives:

- `apps/web/tabs.py` — `TabSpec.min_plan` marks a tab paid, and
  `visible_for(kind, plan)` drops it from the tabbar.
- `apps/web/_gating.py` — `refuse_if_unpaid(request, feature=...)`, called
  as the first statement of every paid route across **all three** route
  families (pages, `_frag` fragments, `/api`). Hiding a tab is not access
  control; the URL stays typeable.
- `services/watchlist.py` — `FREE_WATCHLIST_LIMIT`, counted on distinct
  tickers across all of an account's lists. Enforced in the service, not
  the route, because the TUI adds items too.

`tests/test_gating.py` walks every paid tab across all three route
families and asserts a free caller is refused on each. Adding a paid tab
without a guard fails that test.

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


## Try it (PR-Z demo — Flutterwave billing)

Paid periods are bought through Flutterwave's hosted checkout, in XOF,
**one payment per period**. Flutterwave's recurring "payment plans" pin the
checkout to card, which would exclude Orange Money, Wave and MTN MoMo —
most customers here — so nothing auto-renews: a customer pays for 1 month
or 12 months, paying again *extends* the current period, and reminder
mail goes out 7 days and 1 day before it ends. The plan reads as free the
moment the period ends; an hourly job stamps it `expired` and says so
once by email.

```bash
# .env — test keys from the sandbox account (prefixed _TEST); prices in
# INTEGER francs, XOF is zero-decimal.
FLW_PUBLIC_KEY=FLWPUBK_TEST-...
FLW_SECRET_KEY=FLWSECK_TEST-...
FLW_ENCRYPTION_KEY=FLWSECK_TEST...
FLW_WEBHOOK_HASH=<the "secret hash" you set on Settings → Webhooks>
PRICE_MONTH_XOF=12000
PRICE_YEAR_XOF=120000
```

```bash
just migrate                # 0021_payments
just dev                    # sign in, then /pricing → "Pay 1 month"
```

The flow: `POST /billing/checkout` (signed in, Origin-checked) records a
`pending` payment with a `tx_ref` we mint and 303s to the hosted page.
The customer comes back on `GET /billing/return`; the webhook lands on
`POST /billing/webhook` authenticated by the `verif-hash` header. **Both
are hints, not proof**: the plan is activated only after
`GET /v3/transactions/{id}/verify` says `successful`, `XOF`, amount ≥
price, same `tx_ref`. Activation is idempotent, so redirect and webhook
can both arrive in any order. `/billing` shows the account's plan, period
end and payment history.

Test mode: any mobile number with OTP `123456` mocks a successful mobile
money payment; test cards are in Flutterwave's docs. Without keys the
pricing page says checkout is not open and the webhook answers 401.

## Ops — the job watchdog (PR-AB)

An uptime monitor on `/health` says whether the process answers. It says
nothing about whether the 15:45 brief actually ran. The watchdog does.

Every scheduled job is wrapped so each run lands in `job_runs` (start,
finish, `ok` / `skipped` / `failed`, a one-line note). Every 15 minutes
the `job_watchdog` job asks each job's **own cron trigger** for its
recent due times and compares them with that table, so a job added to
`build_scheduler` is covered automatically. It reports three kinds of
problem:

- **missed** — the due time passed and no run was recorded. Typically a
  restart across the cron minute: APScheduler's in-memory store forgets
  a fire time the moment the process dies.
- **failed** — the job raised. Daily and weekly jobs are reported on the
  first failure; jobs that fire at least hourly get three strikes so a
  single scraper timeout is not an alert.
- **stuck** — a run started and never finished (hung, or the process was
  killed mid-run — the next pass closes such runs as `interrupted`).

Each problem is announced once when it appears, once a day while it
lasts (`OPS_ALERT_REPEAT_HOURS`), and once when it clears. Channels:

```bash
OPS_ALERT_EMAIL=you@example.ci      # through the sign-in mailer (Resend)
DISCORD_WEBHOOK_URL=https://...     # the alerts webhook doubles as ops
```

With neither set, the alert is an `ERROR` line in the journal. Look at
the state from the shell any time — both are read-only and safe beside
the running service:

```bash
just jobs-status    # every job: next due, last run, status, duration, note
just jobs-check     # what the watchdog would flag right now; exit 1 if anything
```

`/health` carries the summary for the external monitor:

```json
"jobs": {"status": "ok", "open": [], "checked_utc": "2026-09-10T15:45:12Z"}
```

`status` is `degraded` while a problem is open, `stale` when the
watchdog's own heartbeat is older than 45 minutes (the scheduler thread
died while uvicorn kept answering), and `unknown` when the DB cannot be
read. Point a keyword monitor at it (see the deploy runbook). Only
problem keys are exposed — the endpoint is public and failure notes can
contain exception text.

Daily and weekly jobs also get a 30-minute misfire grace, so a job whose
cron minute fell while the executor was busy runs late instead of
tomorrow.

## Deploy

The production runbook — a 4 GB Vultr VPS behind Cloudflare, Caddy with an
origin certificate, systemd, the `.env` diff, the owner claim, smoke
tests, day-2 operations — is
[`docs/deploy-kodji-app.md`](./docs/deploy-kodji-app.md). It is written
to be followed top to bottom.

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
src/kodji/
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
