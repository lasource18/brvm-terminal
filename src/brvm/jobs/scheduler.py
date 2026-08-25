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
from brvm.services.alerts import deliver_pending as deliver_alerts
from brvm.services.alerts import evaluate_all as evaluate_alerts
from brvm.services.brief import generate_for as generate_brief
from brvm.services.company_facts import refresh_all as refresh_company_facts
from brvm.services.enrichment import enrich_sectors
from brvm.services.fundamentals import extract_pending
from brvm.services.history import backfill_all as backfill_history
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


def _history_backfill_job() -> None:
    """Weekly historique pass over every active equity so the Directory's
    period-return columns render for the whole universe (not just the
    tickers a user has clicked into). Idempotent within min_age_days=7."""
    log.info("scheduled history backfill start")
    try:
        counts = backfill_history()
        log.info("scheduled history backfill ok: %s", counts)
    except Exception as e:  # pragma: no cover - defensive; job must not kill scheduler
        log.exception("scheduled history backfill failed: %s", e)


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


def _company_facts_refresh_job() -> None:
    """Weekly refresh of sikafinance company facts (shares outstanding, float
    %, market cap) that feed the ratios engine. Only touches rows older
    than a week, so a rerun within the window is cheap."""
    log.info("scheduled company-facts refresh start")
    try:
        counts = refresh_company_facts()
        log.info("scheduled company-facts refresh ok: %s", counts)
    except Exception as e:  # pragma: no cover - defensive; job must not kill scheduler
        log.exception("scheduled company-facts refresh failed: %s", e)


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


def _alerts_evaluate_job() -> None:
    """Fire rule-matching events into `alert_events`. Trails each news
    tag pass so a tagged item can immediately produce an alert."""
    log.info("scheduled alerts eval start (market_open=%s)", is_market_open())
    try:
        counts = evaluate_alerts()
        log.info("scheduled alerts eval ok: %s", counts.as_dict())
    except Exception as e:  # pragma: no cover - defensive; job must not kill scheduler
        log.exception("scheduled alerts eval failed: %s", e)


def _alerts_deliver_job() -> None:
    """Drain the queued events via Discord webhook. No-ops (with a
    warning) when DISCORD_WEBHOOK_URL is unset."""
    log.info("scheduled alerts deliver start")
    try:
        counts = deliver_alerts()
        log.info("scheduled alerts deliver ok: %s", counts.as_dict())
    except Exception as e:  # pragma: no cover - defensive; job must not kill scheduler
        log.exception("scheduled alerts deliver failed: %s", e)


def _brief_job() -> None:
    """Post-close daily brief. Runs Mon-Fri at 15:30 Africa/Abidjan, ~30
    min after the exchange close, so the last snapshot cycle has landed
    and the news tagger has stamped the day's relevance scores."""
    log.info("scheduled brief run start")
    try:
        result = generate_brief()
        log.info("scheduled brief run ok: %s", result.as_dict())
    except Exception as e:  # pragma: no cover - defensive; job must not kill scheduler
        log.exception("scheduled brief run failed: %s", e)


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
    # Company facts (shares_outstanding / float % / market cap): weekly
    # (Sun 04:30 Abidjan, right after sector enrichment). Numbers shift
    # on share splits and issuance, not daily.
    sched.add_job(
        _company_facts_refresh_job,
        CronTrigger(day_of_week="sun", hour="4", minute="30", timezone=str(ABIDJAN)),
        id="company_facts_refresh_weekly",
        replace_existing=True,
    )
    # History backfill: weekly (Sun 05:00 Abidjan). ~48 equities x 0.5s
    # polite pause = <1 min of wall time; keeps daily_bars populated for
    # every ticker so the Directory's period-return columns render for
    # the whole universe.
    sched.add_job(
        _history_backfill_job,
        CronTrigger(day_of_week="sun", hour="5", minute="0", timezone=str(ABIDJAN)),
        id="history_backfill_weekly",
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
    # Alerts evaluator: every 15 min during market hours (offset +11 min
    # from news poll so the tagger has finished stamping relevance), hourly
    # otherwise. The read side is DB-only so there's no rate-limit concern.
    sched.add_job(
        _alerts_evaluate_job,
        CronTrigger(day_of_week="mon-fri", hour="9-14", minute="11-59/15", timezone=str(ABIDJAN)),
        id="alerts_evaluate_market_hours",
        replace_existing=True,
    )
    sched.add_job(
        _alerts_evaluate_job,
        CronTrigger(hour="*", minute="41", timezone=str(ABIDJAN)),
        id="alerts_evaluate_hourly_outside",
        replace_existing=True,
    )
    # Delivery: every 5 min, always on. Cheap when the queue is empty
    # (one indexed COUNT). Batch-capped by settings.alerts_delivery_batch
    # so a webhook outage doesn't turn recovery into a flood.
    sched.add_job(
        _alerts_deliver_job,
        CronTrigger(minute="*/5", timezone=str(ABIDJAN)),
        id="alerts_deliver_every_5min",
        replace_existing=True,
    )
    # Daily brief: Mon-Fri 15:30 Abidjan. BRVM closes ~15:00, and the
    # news tag pass runs on the :23/:38/:53 (etc.) minute inside the
    # market-hours block — 30 min buffer covers both.
    sched.add_job(
        _brief_job,
        CronTrigger(day_of_week="mon-fri", hour="15", minute="30", timezone=str(ABIDJAN)),
        id="brief_daily",
        replace_existing=True,
    )
    return sched
