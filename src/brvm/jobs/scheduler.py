"""APScheduler bootstrap.

Market hours per CLAUDE.md are ~09:00-15:00 Africa/Abidjan, Mon-Fri.
This module wires the jobs but is NOT started by the FastAPI app yet
(Phase 2 will boot it as a background scheduler alongside uvicorn).
"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from brvm.clock import ABIDJAN, is_market_open
from brvm.logging import get
from brvm.services.enrichment import enrich_sectors
from brvm.services.fundamentals import extract_pending
from brvm.services.news import poll_all as poll_news
from brvm.services.ocr import ocr_pending
from brvm.services.quotes import snapshot_once
from brvm.services.tagging import tag_pending

log = get(__name__)


def _snapshot_job() -> None:
    stale = not is_market_open()
    log.info("scheduled snapshot start (market_open=%s)", not stale)
    try:
        counts = snapshot_once()
        log.info("scheduled snapshot ok: %s", counts)
    except Exception as e:  # pragma: no cover - defensive; job must not kill scheduler
        log.exception("scheduled snapshot failed: %s", e)


def _news_job() -> None:
    log.info("scheduled news poll start (market_open=%s)", is_market_open())
    try:
        counts = poll_news()
        log.info("scheduled news poll ok: %s", counts)
    except Exception as e:  # pragma: no cover - defensive; job must not kill scheduler
        log.exception("scheduled news poll failed: %s", e)


def _tag_job() -> None:
    """Tag freshly-polled news with Haiku. No-ops (with a warning) when
    ANTHROPIC_API_KEY is unset or the day's $1 budget is spent, so this is
    safe to register unconditionally."""
    log.info("scheduled news tagging start")
    try:
        counts = tag_pending()
        log.info("scheduled news tagging ok: %s", counts)
    except Exception as e:  # pragma: no cover - defensive; job must not kill scheduler
        log.exception("scheduled news tagging failed: %s", e)


def _sector_enrich_job() -> None:
    log.info("scheduled sector enrichment start")
    try:
        counts = enrich_sectors()
        log.info("scheduled sector enrichment ok: %s", counts)
    except Exception as e:  # pragma: no cover - defensive; job must not kill scheduler
        log.exception("scheduled sector enrichment failed: %s", e)


def _fundamentals_extract_job() -> None:
    """Extract structured fundamentals from unprocessed annual filings.
    No-ops (with a warning) when ANTHROPIC_API_KEY is unset or the day's
    $2 budget is spent, so this is safe to register unconditionally."""
    log.info("scheduled fundamentals extraction start")
    try:
        counts = extract_pending()
        log.info("scheduled fundamentals extraction ok: %s", counts.as_dict())
    except Exception as e:  # pragma: no cover - defensive; job must not kill scheduler
        log.exception("scheduled fundamentals extraction failed: %s", e)


def _filings_ocr_job() -> None:
    """OCR scanned filings so the next extraction pass can pick them up.
    No-ops (with a warning) when the `ocrmypdf` binary isn't installed,
    so this is safe to register unconditionally on any host."""
    log.info("scheduled filings OCR start")
    try:
        counts = ocr_pending()
        log.info("scheduled filings OCR ok: %s", counts.as_dict())
    except Exception as e:  # pragma: no cover - defensive; job must not kill scheduler
        log.exception("scheduled filings OCR failed: %s", e)


def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=str(ABIDJAN))
    # Every 10 minutes during market hours.
    sched.add_job(
        _snapshot_job,
        CronTrigger(day_of_week="mon-fri", hour="9-14", minute="*/10", timezone=str(ABIDJAN)),
        id="snapshot_market_hours",
        replace_existing=True,
    )
    # Hourly outside market hours (weekday nights + weekends) for freshness.
    sched.add_job(
        _snapshot_job,
        CronTrigger(hour="*", minute="17", timezone=str(ABIDJAN)),
        id="snapshot_hourly_outside",
        replace_existing=True,
    )
    # News poll: every 15 min during market hours, hourly otherwise.
    sched.add_job(
        _news_job,
        CronTrigger(day_of_week="mon-fri", hour="9-14", minute="*/15", timezone=str(ABIDJAN)),
        id="news_market_hours",
        replace_existing=True,
    )
    sched.add_job(
        _news_job,
        CronTrigger(hour="*", minute="23", timezone=str(ABIDJAN)),
        id="news_hourly_outside",
        replace_existing=True,
    )
    # News tagging: trails each news poll by ~7 min so the freshly-ingested
    # rows are picked up in the same cycle (poll runs at */15 and :23).
    sched.add_job(
        _tag_job,
        CronTrigger(day_of_week="mon-fri", hour="9-14", minute="7-59/15", timezone=str(ABIDJAN)),
        id="news_tag_market_hours",
        replace_existing=True,
    )
    sched.add_job(
        _tag_job,
        CronTrigger(hour="*", minute="31", timezone=str(ABIDJAN)),
        id="news_tag_hourly_outside",
        replace_existing=True,
    )
    # Sector backfill: weekly (Sun 04:00 Abidjan). Sikafinance sector rarely
    # changes and 47 per-ticker requests would be rude to run often.
    sched.add_job(
        _sector_enrich_job,
        CronTrigger(day_of_week="sun", hour="4", minute="0", timezone=str(ABIDJAN)),
        id="sector_enrichment_weekly",
        replace_existing=True,
    )
    # OCR sweep: daily at 02:00 Abidjan, one hour before the extraction
    # job so newly-OCR'd filings land in the same night's cycle. Runs
    # under the `settings.ocr_max_files_per_run` cap so a large scanned
    # backlog won't eat the whole hour.
    sched.add_job(
        _filings_ocr_job,
        CronTrigger(hour="2", minute="0", timezone=str(ABIDJAN)),
        id="filings_ocr_daily",
        replace_existing=True,
    )
    # Fundamentals extraction: daily at 03:00 Abidjan (well after market
    # close, before the sector job) so the $2 budget lands on the same UTC
    # day as `filings_spend` accounting.
    sched.add_job(
        _fundamentals_extract_job,
        CronTrigger(hour="3", minute="0", timezone=str(ABIDJAN)),
        id="fundamentals_extract_daily",
        replace_existing=True,
    )
    return sched
