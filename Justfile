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
fundamentals-extract:
    uv run python -m brvm.jobs.fundamentals_extract --once

# Same, without spending anything: shows what would be sent and the estimated cost.
fundamentals-extract-dry:
    uv run python -m brvm.jobs.fundamentals_extract --once --dry-run
