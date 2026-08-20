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
from brvm.services.news import poll_all as poll_news
from brvm.services.quotes import snapshot_once

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


def _sector_enrich_job() -> None:
    log.info("scheduled sector enrichment start")
    try:
        counts = enrich_sectors()
        log.info("scheduled sector enrichment ok: %s", counts)
    except Exception as e:  # pragma: no cover - defensive; job must not kill scheduler
        log.exception("scheduled sector enrichment failed: %s", e)


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
    # Sector backfill: weekly (Sun 04:00 Abidjan). Sikafinance sector rarely
    # changes and 47 per-ticker requests would be rude to run often.
    sched.add_job(
        _sector_enrich_job,
        CronTrigger(day_of_week="sun", hour="4", minute="0", timezone=str(ABIDJAN)),
        id="sector_enrichment_weekly",
        replace_existing=True,
    )
    return sched
