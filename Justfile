set shell := ["bash", "-cu"]

default:
    @just --list

# Install / sync all deps into .venv via uv
sync:
    uv sync --all-groups

# Run the FastAPI web app with hot reload (port 8765 to avoid clashing with
# another local project on 8000; change here + docker-compose/systemd if
# you want a different one).
dev:
    uv run uvicorn kodji.apps.web.main:app --reload --host 127.0.0.1 --port 8765

# Phase 5: run the Textual TUI. Shares the DB + services layer with the
# web app; refresh polls every 30s during market hours.
tui:
    uv run python -m kodji.apps.tui

# Apply SQL migrations to $DB_PATH (default ./data/kodji.sqlite)
migrate:
    uv run python scripts/migrate.py

# Run tests (offline, fixture-based)
test:
    uv run pytest

# Lint
lint:
    uv run ruff check .

# Format
fmt:
    uv run ruff format .

# Refresh HTML/PDF fixtures against the live web (dev-only)
refresh-fixtures:
    uv run python scripts/refresh_fixtures.py

# Phase 1 demo: refresh securities + one snapshot cycle, print top-10 by turnover
snapshot:
    uv run python -m kodji.jobs.quote_snapshot --once

# Phase 8 demo: fetch brvm.org bond listings (state / regional / private)
# and upsert them as `kind='bond'` securities + daily_bars rows.
bonds-poll:
    uv run python -m kodji.jobs.bonds_snapshot --once

# F-04: cross-check `daily_bars.close` against the official BOC PDF for
# the day the PDF actually covers. Read-only — prints per-ticker drift.
boc-reconcile:
    uv run python -m kodji.jobs.boc_reconcile --once

# F-23: one-shot migration for filings.file_path stored as absolute
# paths. Run once after upgrading to a build that persists relative
# paths going forward, and again before a Mac → VPS deploy so the
# corpus stays portable. Dry-run by default; pass APPLY=1 to commit.
filings-paths-rewrite:
    uv run python scripts/rewrite_filings_paths.py {{ if env_var_or_default("APPLY", "") == "" { "" } else { "--apply" } }}

# Bulk-populate daily_bars for every equity so the Directory's period-return
# columns (1W/1M/3M/YTD/1Y/ALL%) render values for the whole universe rather
# than only the tickers a user has personally clicked into. Idempotent within
# --min-age-days (default 7).
history-backfill:
    uv run python -m kodji.jobs.history_backfill --once

# Phase 3a demo: fetch news feed + communiqués + upcoming dividends, print summary
news-poll:
    uv run python -m kodji.jobs.news_poll --once

# Backfill securities.sector by scraping sikafinance/afx per equity
sector-enrich:
    uv run python -m kodji.jobs.sector_enrich --once

# Phase 3b demo: tag untagged news items with Haiku (respects the $1/day cap)
news-tag:
    uv run python -m kodji.jobs.news_tag --once

# Same, without spending anything: shows batch shape + prompt size only
news-tag-dry:
    uv run python -m kodji.jobs.news_tag --once --dry-run

# Phase 4a demo: walk brvm.org issuers + download new filing PDFs to data/filings/
# Optional MAX_ISSUERS=<n> just filings-pull    → limit issuers walked (smoke run)
# Optional ONLY_TICKERS=SNTS,ORAC just filings-pull → fetch a specific subset
#   only (walk still visits every issuer on the index but only these tickers
#   are downloaded — useful for backfilling one issuer after a fetcher fix).
filings-pull:
    uv run python -m kodji.jobs.filings_pull --once {{ if env_var_or_default("MAX_ISSUERS", "") == "" { "" } else { "--max-issuers " + env_var("MAX_ISSUERS") } }} {{ if env_var_or_default("ONLY_TICKERS", "") == "" { "" } else { "--only-tickers " + env_var("ONLY_TICKERS") } }}

# Phase 4b: extract structured fundamentals from unprocessed annual filings.
# Respects the $2/day cap (LLM_EXTRACT_DAILY_CAP_CENTS).
# Optional LIMIT=<n> just fundamentals-extract  → cap filings considered
# this pass. Default 200; the budget cap is still the real gate on spend.
fundamentals-extract:
    uv run python -m kodji.jobs.fundamentals_extract --once {{ if env_var_or_default("LIMIT", "") == "" { "" } else { "--limit " + env_var("LIMIT") } }}

# Same, without spending anything: shows what would be sent and the estimated cost.
fundamentals-extract-dry:
    uv run python -m kodji.jobs.fundamentals_extract --once --dry-run

# One-shot recovery for ownership/segments that were wiped before PR #13's
# fix landed. Clears extracted_utc on shadowed filings so the next
# `just fundamentals-extract` run re-populates them.
fundamentals-recover:
    uv run python -m kodji.jobs.fundamentals_recover --once

# Same, without touching the DB — reports what would be reset.
fundamentals-recover-dry:
    uv run python -m kodji.jobs.fundamentals_recover --once --dry-run

# Phase 7: one-shot recovery pass that clears extracted_utc on filings
# whose persisted `financials` row is missing every cash-flow column.
# The next `just fundamentals-extract` re-runs the Phase-7 prompt against
# them and populates cash_flow_ops / capex / free_cash_flow. Respects
# the daily extraction cap. Idempotent.
fundamentals-recover-cashflow:
    uv run python -m kodji.jobs.fundamentals_recover --once --cash-flow

fundamentals-recover-cashflow-dry:
    uv run python -m kodji.jobs.fundamentals_recover --once --cash-flow --dry-run

# Phase 4c: OCR scanned filings so 4b can extract them.
# Requires the `ocrmypdf` binary and tesseract with the French language pack
# (brew install ocrmypdf tesseract-lang).
filings-ocr:
    uv run python -m kodji.jobs.filings_ocr --once

# Phase 4d: refresh sikafinance-sourced company facts (shares outstanding,
# float %, market cap) that the ratios engine uses. Runs weekly on the
# scheduler; use this to trigger a manual pass.
company-refresh:
    uv run python -m kodji.jobs.company_refresh --once

# Phase 6a: evaluate every enabled alert rule and queue matching events.
# Idempotent — a re-eval before new data lands is a no-op via the
# (rule_id, dedupe_key) UNIQUE in alert_events.
alerts-eval:
    uv run python -m kodji.jobs.alerts_evaluate --once

# Phase 6a: drain the alert_events queue via the Discord webhook.
# No-ops when DISCORD_WEBHOOK_URL is unset (events land as 'skipped').
alerts-deliver:
    uv run python -m kodji.jobs.alerts_deliver --once

# Phase 6b: generate today's post-close brief and persist it to `briefs`.
# Respects the $0.50/day cap (BRIEF_DAILY_CAP_CENTS). Runs Mon-Fri at
# 15:30 Abidjan on the scheduler; use this to trigger a manual pass.
# Regenerate the product screenshots in screenshots/ (French by default).
# `just screenshots --locale en` for the English set.
screenshots *args:
    uv run python scripts/screenshots.py {{args}}

# Fill in missing FR translations on briefs + analyst notes. Costs Haiku
# calls, billed to the same daily counters as a normal brief/note run.
# `just backfill-translations --dry-run` to see what is pending first.
backfill-translations *args:
    uv run python scripts/backfill_translations.py {{args}}

# Extra args pass through: `just brief-run --date 2026-08-20`.
brief-run *args:
    uv run python -m kodji.jobs.brief_run --once {{args}}

# Same, without spending anything: prints the context shape only.
# Extra args pass through: `just brief-run-dry --date 2026-08-20`.
brief-run-dry *args:
    uv run python -m kodji.jobs.brief_run --once --dry-run {{args}}

# Phase 6c: generate this week's per-ticker analyst notes (Sonnet).
# Respects the $3/day cap (NOTES_DAILY_CAP_CENTS). Runs Sat 20:00
# Abidjan on the scheduler; use this to trigger a manual pass.
# Extra args pass through:
#   `just analyst-notes-run --ticker SNTS`   → one ticker only
#   `just analyst-notes-run --limit 5`       → smoke run
#   `just analyst-notes-run --week 2026-08-24`
analyst-notes-run *args:
    uv run python -m kodji.jobs.analyst_notes_run --once {{args}}

# Same, without spending anything: reports the plan per ticker.
analyst-notes-run-dry *args:
    uv run python -m kodji.jobs.analyst_notes_run --once --dry-run {{args}}
