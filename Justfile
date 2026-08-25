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
    uv run uvicorn brvm.apps.web.main:app --reload --host 127.0.0.1 --port 8765

# Apply SQL migrations to $DB_PATH (default ./data/brvm.sqlite)
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
    uv run python -m brvm.jobs.quote_snapshot --once

# Bulk-populate daily_bars for every equity so the Directory's period-return
# columns (1W/1M/3M/YTD/1Y/ALL%) render values for the whole universe rather
# than only the tickers a user has personally clicked into. Idempotent within
# --min-age-days (default 7).
history-backfill:
    uv run python -m brvm.jobs.history_backfill --once

# Phase 3a demo: fetch news feed + communiqués + upcoming dividends, print summary
news-poll:
    uv run python -m brvm.jobs.news_poll --once

# Backfill securities.sector by scraping sikafinance/afx per equity
sector-enrich:
    uv run python -m brvm.jobs.sector_enrich --once

# Phase 3b demo: tag untagged news items with Haiku (respects the $1/day cap)
news-tag:
    uv run python -m brvm.jobs.news_tag --once

# Same, without spending anything: shows batch shape + prompt size only
news-tag-dry:
    uv run python -m brvm.jobs.news_tag --once --dry-run

# Phase 4a demo: walk brvm.org issuers + download new filing PDFs to data/filings/
# Optional MAX_ISSUERS=<n> just filings-pull   → limit issuers walked (smoke run)
filings-pull:
    uv run python -m brvm.jobs.filings_pull --once {{ if env_var_or_default("MAX_ISSUERS", "") == "" { "" } else { "--max-issuers " + env_var("MAX_ISSUERS") } }}

# Phase 4b: extract structured fundamentals from unprocessed annual filings.
# Respects the $2/day cap (LLM_EXTRACT_DAILY_CAP_CENTS).
# Optional LIMIT=<n> just fundamentals-extract  → cap filings considered
# this pass. Default 200; the budget cap is still the real gate on spend.
fundamentals-extract:
    uv run python -m brvm.jobs.fundamentals_extract --once {{ if env_var_or_default("LIMIT", "") == "" { "" } else { "--limit " + env_var("LIMIT") } }}

# Same, without spending anything: shows what would be sent and the estimated cost.
fundamentals-extract-dry:
    uv run python -m brvm.jobs.fundamentals_extract --once --dry-run

# One-shot recovery for ownership/segments that were wiped before PR #13's
# fix landed. Clears extracted_utc on shadowed filings so the next
# `just fundamentals-extract` run re-populates them.
fundamentals-recover:
    uv run python -m brvm.jobs.fundamentals_recover --once

# Same, without touching the DB — reports what would be reset.
fundamentals-recover-dry:
    uv run python -m brvm.jobs.fundamentals_recover --once --dry-run

# Phase 4c: OCR scanned filings so 4b can extract them.
# Requires the `ocrmypdf` binary and tesseract with the French language pack
# (brew install ocrmypdf tesseract-lang).
filings-ocr:
    uv run python -m brvm.jobs.filings_ocr --once

# Phase 4d: refresh sikafinance-sourced company facts (shares outstanding,
# float %, market cap) that the ratios engine uses. Runs weekly on the
# scheduler; use this to trigger a manual pass.
company-refresh:
    uv run python -m brvm.jobs.company_refresh --once

# Phase 6a: evaluate every enabled alert rule and queue matching events.
# Idempotent — a re-eval before new data lands is a no-op via the
# (rule_id, dedupe_key) UNIQUE in alert_events.
alerts-eval:
    uv run python -m brvm.jobs.alerts_evaluate --once

# Phase 6a: drain the alert_events queue via the Discord webhook.
# No-ops when DISCORD_WEBHOOK_URL is unset (events land as 'skipped').
alerts-deliver:
    uv run python -m brvm.jobs.alerts_deliver --once

# Phase 6b: generate today's post-close brief and persist it to `briefs`.
# Respects the $0.50/day cap (BRIEF_DAILY_CAP_CENTS). Runs Mon-Fri at
# 15:30 Abidjan on the scheduler; use this to trigger a manual pass.
# Extra args pass through: `just brief-run --date 2026-08-20`.
brief-run *args:
    uv run python -m brvm.jobs.brief_run --once {{args}}

# Same, without spending anything: prints the context shape only.
# Extra args pass through: `just brief-run-dry --date 2026-08-20`.
brief-run-dry *args:
    uv run python -m brvm.jobs.brief_run --once --dry-run {{args}}

# Phase 6c: generate this week's per-ticker analyst notes (Sonnet).
# Respects the $3/day cap (NOTES_DAILY_CAP_CENTS). Runs Sat 20:00
# Abidjan on the scheduler; use this to trigger a manual pass.
# Extra args pass through:
#   `just analyst-notes-run --ticker SNTS`   → one ticker only
#   `just analyst-notes-run --limit 5`       → smoke run
#   `just analyst-notes-run --week 2026-08-24`
analyst-notes-run *args:
    uv run python -m brvm.jobs.analyst_notes_run --once {{args}}

# Same, without spending anything: reports the plan per ticker.
analyst-notes-run-dry *args:
    uv run python -m brvm.jobs.analyst_notes_run --once --dry-run {{args}}
