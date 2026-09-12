"""APScheduler bootstrap.

Market hours per CLAUDE.md are ~09:00-15:00 Africa/Abidjan, Mon-Fri.
`build_scheduler` wires every job; the FastAPI lifespan starts it.

Every body registered here goes through `services/watchdog.tracked`,
which logs start/ok/failed, records the run in `job_runs`, and swallows
exceptions so a job can never kill the scheduler. Bodies therefore just
*return* a summary (a dict, or anything with `as_dict()`), raise on
failure, or return `Skipped(reason)` for a deliberate no-op. The watchdog
job at the end of the list reads those rows back and alerts when a job
silently failed to run (PR-AB).
"""

from __future__ import annotations

from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from kodji.clock import ABIDJAN, is_market_holiday, session_date_for, utcnow
from kodji.logging import get
from kodji.services.alerts import deliver_pending as deliver_alerts
from kodji.services.alerts import evaluate_all as evaluate_alerts
from kodji.services.analyst_notes import generate_for_all as generate_analyst_notes
from kodji.services.auth import purge_expired as purge_expired_sessions
from kodji.services.billing import expire_lapsed as expire_lapsed_plans
from kodji.services.billing import remind_expiring as remind_expiring_plans
from kodji.services.brief import generate_for as generate_brief
from kodji.services.company_facts import refresh_all as refresh_company_facts
from kodji.services.enrichment import enrich_sectors
from kodji.services.filings import pull_all as pull_filings
from kodji.services.fundamentals import extract_pending
from kodji.services.history import backfill_all as backfill_history
from kodji.services.news import poll_all as poll_news
from kodji.services.ocr import ocr_pending
from kodji.services.quotes import snapshot_bonds_once, snapshot_once
from kodji.services.reconcile import check_boc_close
from kodji.services.tagging import tag_pending
from kodji.services.watchdog import WATCHDOG_JOB_ID, JobSpec, Skipped, run_check, tracked

log = get(__name__)

# Misfire grace for the once-a-day and once-a-week jobs. APScheduler's
# default (1 s) drops a run outright if the executor was busy at the cron
# minute; a daily job that then runs 20 minutes late is far better than
# one that runs tomorrow. Frequent jobs keep the 60 s scheduler default
# below — the next tick is minutes away anyway.
DAILY_MISFIRE_GRACE_S = 30 * 60


def _snapshot_job() -> dict:
    return snapshot_once()


def _bonds_snapshot_job() -> dict:
    """Refresh brvm.org bond listings once per weekday post-close. Bond
    prices update at most daily on the exchange page, so a single pass
    after 15:00 Abidjan is enough — no need to run intraday."""
    return snapshot_bonds_once()


def _news_job() -> dict:
    return poll_news()


def _tag_job() -> dict:
    """Tag freshly-polled news with Haiku. No-ops (with a warning) when
    ANTHROPIC_API_KEY is unset or the day's $1 budget is spent, so this is
    safe to register unconditionally."""
    return tag_pending()


def _sector_enrich_job() -> dict:
    return enrich_sectors()


def _history_backfill_job() -> dict:
    """Weekly historique pass over every active equity so the Directory's
    period-return columns render for the whole universe (not just the
    tickers a user has clicked into). Idempotent within min_age_days=7."""
    return backfill_history()


def _fundamentals_extract_job() -> dict:
    """Extract structured fundamentals from unprocessed annual filings.
    No-ops (with a warning) when ANTHROPIC_API_KEY is unset or the day's
    $2 budget is spent, so this is safe to register unconditionally."""
    return extract_pending().as_dict()


def _company_facts_refresh_job() -> dict:
    """Weekly refresh of sikafinance company facts (shares outstanding, float
    %, market cap) that feed the ratios engine. Only touches rows older
    than a week, so a rerun within the window is cheap."""
    return refresh_company_facts()


def _filings_pull_job() -> dict:
    """F-27: walk brvm.org issuers and download new filing PDFs so the
    OCR + extraction pipeline has fresh material to chew on. Runs
    ahead of the OCR sweep (02:00 Abidjan) so a nightly cycle can go
    pull → OCR → extract in one pass. The 0.5s per-PDF politeness
    pause is enforced inside `pull_all` (see F-27 fix)."""
    return pull_filings()


def _filings_ocr_job() -> dict:
    """OCR scanned filings so the next extraction pass can pick them up.
    No-ops (with a warning) when the `ocrmypdf` binary isn't installed,
    so this is safe to register unconditionally on any host."""
    return ocr_pending().as_dict()


def _alerts_evaluate_job() -> dict:
    """Fire rule-matching events into `alert_events`. Trails each news
    tag pass so a tagged item can immediately produce an alert."""
    return evaluate_alerts().as_dict()


def _alerts_deliver_job() -> dict:
    """Drain the queued events: Web Push to every device a member
    enabled, email to members with none. No-ops (with a warning) when
    neither VAPID keys nor email are configured."""
    return deliver_alerts().as_dict()


def _brief_job() -> dict | Skipped:
    """Post-close daily brief. Runs Mon-Fri at 15:45 Africa/Abidjan
    (F-18 shift from 15:30). The next-day tag pass runs at :07 outside
    market hours, but the 15:00 close is followed by the 15:31 hourly
    tagger — 15:45 gives it a 14-min head start so news polled at
    14:45 or 15:00 gets tagged into the day's brief instead of missing
    it. Also skips WAEMU holidays via `is_market_holiday` so a Mon-Fri
    public holiday doesn't produce a "session recap" of the prior
    trading day's stale data."""
    today = session_date_for()
    if is_market_holiday(today):
        return Skipped(f"{today} is a WAEMU holiday")
    return generate_brief().as_dict()


def _boc_reconcile_job() -> dict:
    """F-04: cross-check `daily_bars.close` against the official BOC
    PDF once per weekday, ~30 min after the exchange publishes the day's
    bulletin. Read-only — mismatches land in the log for now; a follow-
    up will route them into `alert_events` once the shape is stable."""
    report = check_boc_close()
    return {
        "session": str(report.session_date),
        "boc_rows": report.boc_rows,
        "matched": report.matched,
        "drift": len(report.drift),
    }


def _analyst_notes_job() -> dict:
    """Weekly per-ticker analyst notes. Runs Sat 20:00 Africa/Abidjan
    so Friday's close, news tags, and Friday's brief have all landed —
    plenty of time for the notes to be ready before Monday's open."""
    return generate_analyst_notes().as_dict()


def _sessions_purge_job() -> dict:
    """Drop expired sessions and spent sign-in challenges.

    Pure housekeeping — an expired session is already unusable, because
    the read path filters on `expires_utc` in SQL. This just stops the
    two tables growing forever on a long-lived install.
    """
    sessions, tokens = purge_expired_sessions()
    return {"sessions": sessions, "login_tokens": tokens}


def _billing_expire_job() -> dict:
    """PR-Z: stamp lapsed paid periods `expired` and tell the customer
    once. Access was already cut by `plan_for`; this is the books."""
    return expire_lapsed_plans()


def _billing_remind_job() -> dict:
    """PR-Z: the 7-day and 1-day renewal reminders, once each."""
    return remind_expiring_plans()


def _watchdog_job(sched: BackgroundScheduler):
    """Closure over the scheduler so the check reads the live job list
    and each job's real trigger — nothing to keep in sync by hand."""

    def body() -> dict:
        jobs = [JobSpec(j.id, j.trigger) for j in sched.get_jobs() if j.id != WATCHDOG_JOB_ID]
        return run_check(jobs).as_dict()

    return body


def _add(sched: BackgroundScheduler, job_id: str, fn, trigger: CronTrigger, **kw) -> None:
    # `name=job_id` so APScheduler's own "Running job ..." lines say
    # `brief_daily`, not `_watchdog_job.<locals>.body`.
    sched.add_job(tracked(job_id, fn), trigger, id=job_id, name=job_id, replace_existing=True, **kw)


def _cron(**fields) -> CronTrigger:
    return CronTrigger(timezone=str(ABIDJAN), **fields)


def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(
        timezone=str(ABIDJAN),
        job_defaults={"coalesce": True, "misfire_grace_time": 60},
    )
    daily = {"misfire_grace_time": DAILY_MISFIRE_GRACE_S}
    # Every 10 minutes during market hours.
    _add(
        sched,
        "snapshot_market_hours",
        _snapshot_job,
        _cron(day_of_week="mon-fri", hour="9-14", minute="*/10"),
    )
    # Hourly outside market hours (weekday nights + weekends) for freshness.
    _add(sched, "snapshot_hourly_outside", _snapshot_job, _cron(hour="*", minute="17"))
    # Bond snapshot: once per weekday at 15:20 Abidjan, ~20 min after close.
    # brvm.org only refreshes bond prices at the end of the session, so
    # intraday polls would just re-fetch identical numbers.
    _add(
        sched,
        "bonds_snapshot_daily",
        _bonds_snapshot_job,
        _cron(day_of_week="mon-fri", hour="15", minute="20"),
        **daily,
    )
    # News poll: every 15 min during market hours, hourly otherwise.
    _add(
        sched,
        "news_market_hours",
        _news_job,
        _cron(day_of_week="mon-fri", hour="9-14", minute="*/15"),
    )
    _add(sched, "news_hourly_outside", _news_job, _cron(hour="*", minute="23"))
    # News tagging: trails each news poll by ~7 min so the freshly-ingested
    # rows are picked up in the same cycle (poll runs at */15 and :23).
    _add(
        sched,
        "news_tag_market_hours",
        _tag_job,
        _cron(day_of_week="mon-fri", hour="9-14", minute="7-59/15"),
    )
    _add(sched, "news_tag_hourly_outside", _tag_job, _cron(hour="*", minute="31"))
    # Sector backfill: weekly (Sun 04:00 Abidjan). Sikafinance sector rarely
    # changes and 47 per-ticker requests would be rude to run often.
    _add(
        sched,
        "sector_enrichment_weekly",
        _sector_enrich_job,
        _cron(day_of_week="sun", hour="4", minute="0"),
        **daily,
    )
    # Company facts (shares_outstanding / float % / market cap): weekly
    # (Sun 04:30 Abidjan, right after sector enrichment). Numbers shift
    # on share splits and issuance, not daily.
    _add(
        sched,
        "company_facts_refresh_weekly",
        _company_facts_refresh_job,
        _cron(day_of_week="sun", hour="4", minute="30"),
        **daily,
    )
    # History backfill: weekly (Sun 05:00 Abidjan). ~48 equities x 0.5s
    # polite pause = <1 min of wall time; keeps daily_bars populated for
    # every ticker so the Directory's period-return columns render for
    # the whole universe.
    _add(
        sched,
        "history_backfill_weekly",
        _history_backfill_job,
        _cron(day_of_week="sun", hour="5", minute="0"),
        **daily,
    )
    # F-27: filings pull daily at 01:00 Abidjan, ahead of OCR (02:00)
    # and extraction (03:00) so a single nightly cycle can go
    # pull → OCR → extract. Walks every brvm.org issuer with the 0.5s
    # per-PDF politeness pause (enforced inside `pull_all`).
    _add(sched, "filings_pull_daily", _filings_pull_job, _cron(hour="1", minute="0"), **daily)
    # OCR sweep: daily at 02:00 Abidjan, one hour before the extraction
    # job so newly-OCR'd filings land in the same night's cycle. Runs
    # under the `settings.ocr_max_files_per_run` cap so a large scanned
    # backlog won't eat the whole hour.
    _add(sched, "filings_ocr_daily", _filings_ocr_job, _cron(hour="2", minute="0"), **daily)
    # Fundamentals extraction: daily at 03:00 Abidjan (well after market
    # close, before the sector job) so the $2 budget lands on the same UTC
    # day as `filings_spend` accounting.
    _add(
        sched,
        "fundamentals_extract_daily",
        _fundamentals_extract_job,
        _cron(hour="3", minute="0"),
        **daily,
    )
    # Alerts evaluator: every 15 min during market hours (offset +11 min
    # from news poll so the tagger has finished stamping relevance), hourly
    # otherwise. The read side is DB-only so there's no rate-limit concern.
    _add(
        sched,
        "alerts_evaluate_market_hours",
        _alerts_evaluate_job,
        _cron(day_of_week="mon-fri", hour="9-14", minute="11-59/15"),
    )
    _add(
        sched, "alerts_evaluate_hourly_outside", _alerts_evaluate_job, _cron(hour="*", minute="41")
    )
    # Delivery: every 5 min, always on. Cheap when the queue is empty
    # (one indexed COUNT). Batch-capped by settings.alerts_delivery_batch
    # so a webhook outage doesn't turn recovery into a flood.
    _add(sched, "alerts_deliver_every_5min", _alerts_deliver_job, _cron(minute="*/5"))
    # Daily brief: Mon-Fri 15:45 Abidjan (F-18 shift). The market-hours
    # tag pass runs :07/:22/:37/:52 within 9-14; the hourly-outside pass
    # fires at :31. News polled at 14:45 or 15:00 (right before close)
    # only gets a relevance stamp at 15:31 — running the brief at 15:30
    # missed that batch, and its `relevance IS NOT NULL` filter dropped
    # end-of-session news from the recap. 15:45 gives 14 min after the
    # hourly-outside tagger fires. `_brief_job` also gates on
    # `is_market_holiday` so a public-holiday Mon-Fri no-ops.
    _add(
        sched,
        "brief_daily",
        _brief_job,
        _cron(day_of_week="mon-fri", hour="15", minute="45"),
        **daily,
    )
    # BOC reconciliation: daily at 16:00 Abidjan — brvm.org typically
    # publishes the day's bulletin between 15:30 and 15:50 after the
    # 15:00 close, so a 16:00 pass reliably lands on the fresh PDF.
    # Weekend/holiday runs no-op (BOC PDF unavailable → warning only).
    _add(
        sched,
        "boc_reconcile_daily",
        _boc_reconcile_job,
        _cron(day_of_week="mon-fri", hour="16", minute="0"),
        **daily,
    )
    # Analyst notes: weekly Saturday 20:00 Abidjan. All the sub-daily
    # jobs (Friday's brief, the news tagger, snapshots) have settled by
    # then, and the notes are ready for Monday's open. A full 47-ticker
    # pass at Sonnet rates ≈ $1.90; NOTES_DAILY_CAP_CENTS gates a rerun
    # from draining the budget.
    _add(
        sched,
        "analyst_notes_weekly",
        _analyst_notes_job,
        _cron(day_of_week="sat", hour="20", minute="0"),
        **daily,
    )
    # Session/challenge purge: daily at 03:30 Abidjan, deep in the quiet
    # window between the hourly snapshot and the morning jobs.
    _add(sched, "sessions_purge_daily", _sessions_purge_job, _cron(hour="3", minute="30"), **daily)
    # PR-Z: lapsed paid periods, hourly at :50 — cheap indexed query, and
    # an expiry should not wait a day to be acknowledged. Reminders once a
    # day at 08:00 Abidjan, when a renewal is most likely to be acted on.
    _add(sched, "billing_expire_hourly", _billing_expire_job, _cron(minute="50"))
    _add(sched, "billing_remind_daily", _billing_remind_job, _cron(hour="8", minute="0"), **daily)
    # PR-AB: the watchdog. Every 15 min, plus one pass shortly after boot
    # so `/health` has a fresh heartbeat and a run interrupted by the
    # restart is closed right away rather than at the next quarter-hour.
    _add(
        sched,
        WATCHDOG_JOB_ID,
        _watchdog_job(sched),
        _cron(minute="*/15"),
        next_run_time=utcnow() + timedelta(seconds=30),
    )
    return sched
