"""PR-AB: the job-missed watchdog.

The check is driven with real APScheduler CronTriggers and fixed clocks,
so what is pinned here is the actual question the operator cares about:
"the brief was due at 15:45 — did it run?"
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from apscheduler.triggers.cron import CronTrigger

from kodji.config import reset_settings_cache
from kodji.db import connect
from kodji.services import watchdog
from kodji.services.mailer import ConsoleMailer
from kodji.services.watchdog import (
    WATCHDOG_JOB_ID,
    JobSpec,
    LastRun,
    Skipped,
    check,
    health_summary,
    last_due_times,
    run_check,
    send_ops_alert,
    tracked,
)
from kodji.store import job_runs as repo

from .conftest import apply_migrations

ABJ = "Africa/Abidjan"


def _cron(**fields) -> CronTrigger:
    return CronTrigger(timezone=ABJ, **fields)


BRIEF = JobSpec("brief_daily", _cron(day_of_week="mon-fri", hour="15", minute="45"))
DELIVER = JobSpec("alerts_deliver_every_5min", _cron(minute="*/5"))
NEWS_HOURLY = JobSpec("news_hourly_outside", _cron(hour="*", minute="23"))
NOTES = JobSpec("analyst_notes_weekly", _cron(day_of_week="sat", hour="20", minute="0"))
OCR = JobSpec("filings_ocr_daily", _cron(hour="2", minute="0"))
WATCHDOG = JobSpec(WATCHDOG_JOB_ID, _cron(minute="*/15"))

SINCE = datetime(2026, 9, 1, tzinfo=UTC)


def at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).replace(tzinfo=UTC)


def run(job_id: str, started: str, status: str = "ok", note: str = "") -> LastRun:
    s = at(started)
    finished = None if status == "running" else s + timedelta(seconds=5)
    return LastRun(job_id=job_id, started=s, finished=finished, status=status, note=note)


# --- last_due_times ---------------------------------------------------------


def test_last_due_daily_weekday_job():
    # Wednesday 2026-09-09, 16:00 UTC (== Abidjan).
    assert last_due_times(BRIEF.trigger, at("2026-09-09T16:00:00")) == [at("2026-09-09T15:45:00")]
    # Saturday noon: the last due was Friday's.
    assert last_due_times(BRIEF.trigger, at("2026-09-12T12:00:00")) == [at("2026-09-11T15:45:00")]
    # Just before today's due time: yesterday's.
    assert last_due_times(BRIEF.trigger, at("2026-09-09T15:44:59")) == [at("2026-09-08T15:45:00")]


def test_last_due_weekly_job_needs_the_wide_window():
    assert last_due_times(NOTES.trigger, at("2026-09-09T12:00:00")) == [at("2026-09-05T20:00:00")]


def test_last_due_returns_the_last_n_oldest_first():
    assert last_due_times(DELIVER.trigger, at("2026-09-09T12:00:00"), 3) == [
        at("2026-09-09T11:50:00"),
        at("2026-09-09T11:55:00"),
        at("2026-09-09T12:00:00"),
    ]


def test_last_due_empty_when_never_due_in_lookback():
    yearly = _cron(month="1", day="1", hour="0", minute="0")
    assert last_due_times(yearly, at("2026-09-09T12:00:00")) == []


# --- check (pure) -----------------------------------------------------------


def _check(jobs, runs, now, since=SINCE, streaks=None):
    return check(jobs, {r.job_id: r for r in runs}, now=now, watch_since=since, streaks=streaks)


def test_daily_job_with_no_run_after_due_is_missed():
    problems = _check([BRIEF], [], at("2026-09-09T16:00:00"))
    assert [p.key for p in problems] == ["brief_daily:missed"]
    assert "last run never" in problems[0].note
    assert "due 2026-09-09T15:45:00Z" in problems[0].note

    stale = run("brief_daily", "2026-09-08T15:45:02")
    problems = _check([BRIEF], [stale], at("2026-09-09T16:00:00"))
    assert [p.key for p in problems] == ["brief_daily:missed"]
    assert "2026-09-08T15:45:02Z (ok)" in problems[0].note


def test_daily_job_is_fine_before_grace_elapses():
    # 15:50 — today's 15:45 run is due but inside MISS_GRACE, so the job
    # is only held to yesterday's, which ran.
    yesterday = run("brief_daily", "2026-09-08T15:45:02")
    assert _check([BRIEF], [yesterday], at("2026-09-09T15:50:00")) == []
    # ...and the moment the grace elapses, today's counts.
    assert [p.key for p in _check([BRIEF], [yesterday], at("2026-09-09T15:55:01"))] == [
        "brief_daily:missed"
    ]


def test_daily_job_that_ran_or_skipped_is_fine():
    ok = run("brief_daily", "2026-09-09T15:45:02")
    assert _check([BRIEF], [ok], at("2026-09-09T16:00:00")) == []
    skipped = run("brief_daily", "2026-09-09T15:45:02", "skipped", "holiday")
    assert _check([BRIEF], [skipped], at("2026-09-09T16:00:00")) == []


def test_daily_job_that_failed_is_reported_at_once():
    failed = run("brief_daily", "2026-09-09T15:45:02", "failed", "RuntimeError: no LLM")
    problems = _check([BRIEF], [failed], at("2026-09-09T16:00:00"), streaks=lambda _: 1)
    assert [p.key for p in problems] == ["brief_daily:failed"]
    assert "RuntimeError: no LLM" in problems[0].note


def test_running_job_is_neither_missed_nor_failed_until_it_overstays():
    running = run("brief_daily", "2026-09-09T15:45:02", "running")
    assert _check([BRIEF], [running], at("2026-09-09T16:00:00")) == []
    problems = _check([BRIEF], [running], at("2026-09-09T16:30:00"))
    assert [p.key for p in problems] == ["brief_daily:stuck"]
    assert "still running after 44m" in problems[0].note


def test_per_job_max_runtime_lets_the_ocr_sweep_run_for_hours():
    running = run("filings_ocr_daily", "2026-09-09T02:00:01", "running")
    assert _check([OCR], [running], at("2026-09-09T05:00:00")) == []
    problems = _check([OCR], [running], at("2026-09-09T06:30:00"))
    assert [p.key for p in problems] == ["filings_ocr_daily:stuck"]


def test_frequent_job_tolerates_two_misses_but_not_three():
    # Due times up to 11:50: 11:40, 11:45, 11:50.
    now = at("2026-09-09T12:00:00")
    assert _check([DELIVER], [run(DELIVER.id, "2026-09-09T11:45:00")], now) == []
    assert _check([DELIVER], [run(DELIVER.id, "2026-09-09T11:40:00")], now) == []
    problems = _check([DELIVER], [run(DELIVER.id, "2026-09-09T11:35:00")], now)
    assert [p.key for p in problems] == ["alerts_deliver_every_5min:missed"]
    assert problems[0].note.startswith("3 consecutive due times")


def test_frequent_job_tolerates_two_failures_but_not_three():
    now = at("2026-09-09T12:00:00")
    failed = run(NEWS_HOURLY.id, "2026-09-09T11:23:00", "failed", "ReadTimeout")
    assert _check([NEWS_HOURLY], [failed], now, streaks=lambda _: 2) == []
    problems = _check([NEWS_HOURLY], [failed], now, streaks=lambda _: 3)
    assert [p.key for p in problems] == ["news_hourly_outside:failed"]
    assert "3 consecutive failure(s)" in problems[0].note


def test_due_times_before_tracking_began_are_not_judged():
    # Tracking started after the 15:45 due time: nothing to say yet.
    assert _check([BRIEF], [], at("2026-09-09T16:00:00"), since=at("2026-09-09T15:50:00")) == []
    # No tracking at all (fresh install).
    assert _check([BRIEF], [], at("2026-09-09T16:00:00"), since=None) == []


def test_weekly_job_missed_last_saturday():
    problems = _check([NOTES], [], at("2026-09-09T12:00:00"))
    assert [p.key for p in problems] == ["analyst_notes_weekly:missed"]
    assert "due 2026-09-05T20:00:00Z" in problems[0].note


def test_a_job_added_by_a_deploy_is_not_missed_for_the_past():
    """The billing jobs shipped with PR-Z: on the first pass after that
    deploy the watchdog reported them missed for cron minutes that
    predated their existence, and emailed. A never-run job is judged
    only against due times after the process that registered it came up."""
    booted = at("2026-09-09T15:50:00")
    # Due 15:45 today, before boot: not judged.
    problems = check(
        [BRIEF], {}, now=at("2026-09-09T16:00:00"), watch_since=SINCE, process_started=booted
    )
    assert problems == []
    # Next day's 15:45 is after boot and still unrun: reported.
    problems = check(
        [BRIEF], {}, now=at("2026-09-10T16:00:00"), watch_since=SINCE, process_started=booted
    )
    assert [p.key for p in problems] == ["brief_daily:missed"]
    # A job that HAS run before is still held across a restart.
    ran = run("brief_daily", "2026-09-08T15:45:02")
    problems = check(
        [BRIEF],
        {ran.job_id: ran},
        now=at("2026-09-09T16:00:00"),
        watch_since=SINCE,
        process_started=booted,
    )
    assert [p.key for p in problems] == ["brief_daily:missed"]


def test_watchdog_never_checks_itself():
    assert _check([WATCHDOG], [], at("2026-09-09T12:00:00")) == []


# --- tracked ----------------------------------------------------------------


@pytest.fixture
def db(monkeypatch, tmp_db_path: Path) -> Path:
    monkeypatch.setenv("DB_PATH", str(tmp_db_path))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    monkeypatch.setenv("OPS_ALERT_EMAIL", "")
    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setenv("EMAIL_FROM", "")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://kodji.test")
    reset_settings_cache()
    with connect(tmp_db_path) as conn:
        apply_migrations(conn)
    yield tmp_db_path
    reset_settings_cache()


class _Counts:
    def as_dict(self):
        return {"tagged": 2}


def test_tracked_records_ok_with_summary(db: Path):
    tracked("news_tag_hourly_outside", lambda: _Counts())()
    with connect(db) as conn:
        row = repo.latest_run(conn, "news_tag_hourly_outside")
    assert row["status"] == "ok"
    assert row["note"] == "{'tagged': 2}"
    assert row["finished_utc"] is not None


def test_tracked_records_failure_and_swallows_it(db: Path):
    def boom():
        raise ValueError("boom")

    tracked("brief_daily", boom)()  # must not raise
    with connect(db) as conn:
        row = repo.latest_run(conn, "brief_daily")
    assert row["status"] == "failed"
    assert row["note"] == "ValueError: boom"


def test_tracked_records_skipped(db: Path):
    tracked("brief_daily", lambda: Skipped("2026-01-01 is a WAEMU holiday"))()
    with connect(db) as conn:
        row = repo.latest_run(conn, "brief_daily")
    assert row["status"] == "skipped"
    assert row["note"] == "2026-01-01 is a WAEMU holiday"


# --- run_check: notify once, nag daily, announce recovery ------------------


class _Sender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str]] = []

    def __call__(self, subject: str, text: str) -> bool:
        self.calls.append((subject, text))
        return self.ok


def _seed_tracking(db: Path, *rows: tuple[str, str, str]) -> None:
    with connect(db) as conn:
        # An old row of some other job so tracking "began" on 1 Sep.
        repo.start_run(conn, "alerts_deliver_every_5min", started_utc="2026-09-01T00:00:00Z")
        repo.finish_run(conn, "alerts_deliver_every_5min", "2026-09-01T00:00:00Z", status="ok")
        for job_id, stamp, status in rows:
            repo.start_run(conn, job_id, started_utc=stamp)
            if status != "running":
                repo.finish_run(conn, job_id, stamp, status=status)


def test_run_check_lifecycle(db: Path, monkeypatch):
    monkeypatch.setattr(watchdog, "PROCESS_STARTED", SINCE)
    _seed_tracking(db)
    send = _Sender()

    # 1. The brief was due at 15:45 and never ran: one notification.
    out = run_check([BRIEF], now=at("2026-09-09T16:00:00"), send=send)
    assert [p.key for p in out.new] == ["brief_daily:missed"]
    assert out.notified is True
    assert len(send.calls) == 1
    subject, text = send.calls[0]
    assert subject == "kodji watchdog: 1 new"
    assert "NEW    brief_daily missed — due 2026-09-09T15:45:00Z, last run never" in text
    assert "open problems now: 1" in text
    assert "https://kodji.test/health" in text
    with connect(db) as conn:
        assert repo.open_problem_keys(conn) == ["brief_daily:missed"]

    # 2. Fifteen minutes later the problem is still there: silence.
    out = run_check([BRIEF], now=at("2026-09-09T16:15:00"), send=send)
    assert out.notified is None
    assert [p.key for p in out.problems] == ["brief_daily:missed"]
    assert len(send.calls) == 1

    # 3. A day later, still open: one reminder.
    out = run_check([BRIEF], now=at("2026-09-10T16:30:00"), send=send)
    assert [p.key for p in out.repeated] == ["brief_daily:missed"]
    assert len(send.calls) == 2
    assert send.calls[1][0] == "kodji watchdog: 1 still open"
    assert "STILL  brief_daily missed" in send.calls[1][1]

    # 4. The operator ran the brief by hand through the scheduler path.
    with connect(db) as conn:
        repo.start_run(conn, "brief_daily", started_utc="2026-09-10T17:00:00Z")
        repo.finish_run(conn, "brief_daily", "2026-09-10T17:00:00Z", status="ok")
    out = run_check([BRIEF], now=at("2026-09-10T17:15:00"), send=send)
    assert out.cleared == ["brief_daily:missed"]
    assert out.problems == []
    assert len(send.calls) == 3
    assert send.calls[2][0] == "kodji watchdog: 1 cleared"
    assert "CLEAR  brief_daily missed — recovered" in send.calls[2][1]
    with connect(db) as conn:
        assert repo.open_problem_keys(conn) == []

    # 5. Nothing open, nothing to say.
    out = run_check([BRIEF], now=at("2026-09-10T17:30:00"), send=send)
    assert out.notified is None
    assert len(send.calls) == 3


def test_run_check_retries_a_failed_send_next_pass(db: Path, monkeypatch):
    monkeypatch.setattr(watchdog, "PROCESS_STARTED", SINCE)
    _seed_tracking(db)
    send = _Sender(ok=False)

    out = run_check([BRIEF], now=at("2026-09-09T16:00:00"), send=send)
    assert out.notified is False
    # Still recorded as open so it is not re-reported as "new"...
    with connect(db) as conn:
        assert repo.open_problem_keys(conn) == ["brief_daily:missed"]
    # ...but due for a repeat immediately, because nobody heard it.
    send.ok = True
    out = run_check([BRIEF], now=at("2026-09-09T16:15:00"), send=send)
    assert [p.key for p in out.repeated] == ["brief_daily:missed"]
    assert out.notified is True
    assert len(send.calls) == 2


def test_run_check_closes_runs_orphaned_by_a_restart(db: Path, monkeypatch):
    monkeypatch.setattr(watchdog, "PROCESS_STARTED", at("2026-09-09T03:00:00"))
    _seed_tracking(db, ("filings_ocr_daily", "2026-09-09T02:00:00Z", "running"))
    send = _Sender()

    out = run_check([OCR], now=at("2026-09-09T03:05:00"), send=send)
    assert out.orphans_closed == 1
    with connect(db) as conn:
        row = repo.latest_run(conn, "filings_ocr_daily")
    assert row["status"] == "failed"
    assert row["note"] == repo.INTERRUPTED_NOTE
    # A daily job that failed is reported on the same pass.
    assert [p.key for p in out.new] == ["filings_ocr_daily:failed"]
    assert repo.INTERRUPTED_NOTE in out.new[0].note


def test_run_check_prunes_old_rows(db: Path, monkeypatch):
    monkeypatch.setattr(watchdog, "PROCESS_STARTED", SINCE)
    with connect(db) as conn:
        repo.start_run(conn, "alerts_deliver_every_5min", started_utc="2026-07-01T00:00:00Z")
        repo.finish_run(conn, "alerts_deliver_every_5min", "2026-07-01T00:00:00Z", status="ok")
    out = run_check([], now=at("2026-09-09T16:00:00"), send=_Sender())
    assert out.pruned == 1


# --- channels ---------------------------------------------------------------


def test_send_ops_alert_without_channels_logs_an_error(db: Path, caplog):
    with caplog.at_level(logging.ERROR, logger="kodji.services.watchdog"):
        assert send_ops_alert("kodji watchdog: 1 new", "NEW brief_daily missed") is True
    assert "NEW brief_daily missed" in caplog.text
    assert "OPS_ALERT_EMAIL" in caplog.text


def test_send_ops_alert_posts_to_discord(db: Path, monkeypatch, httpx_mock):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/api/webhooks/1/x")
    reset_settings_cache()
    httpx_mock.add_response(
        url="https://discord.test/api/webhooks/1/x", method="POST", status_code=204
    )

    assert send_ops_alert("kodji watchdog: 1 new", "NEW brief_daily missed") is True
    req = httpx_mock.get_requests()[0]
    body = __import__("json").loads(req.content)
    assert body["username"] == "kodji-watchdog"
    assert "**kodji watchdog: 1 new**" in body["content"]
    assert "NEW brief_daily missed" in body["content"]


def test_send_ops_alert_reports_a_discord_failure(db: Path, monkeypatch, httpx_mock):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/api/webhooks/1/x")
    reset_settings_cache()
    httpx_mock.add_response(
        url="https://discord.test/api/webhooks/1/x", method="POST", status_code=500
    )
    assert send_ops_alert("s", "t") is False


def test_send_ops_alert_emails_the_operator(db: Path, monkeypatch):
    monkeypatch.setenv("OPS_ALERT_EMAIL", "ops@example.ci")
    reset_settings_cache()
    mailer = ConsoleMailer()
    monkeypatch.setattr(watchdog, "get_mailer", lambda: mailer)

    assert send_ops_alert("kodji watchdog: 1 new", "NEW brief_daily <missed>") is True
    assert len(mailer.sent) == 1
    msg = mailer.sent[0]
    assert msg.to == "ops@example.ci"
    assert msg.subject == "kodji watchdog: 1 new"
    assert msg.text == "NEW brief_daily <missed>"
    assert msg.html == "<pre>NEW brief_daily &lt;missed&gt;</pre>"


# --- /health summary --------------------------------------------------------


def test_health_unknown_when_db_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "nope.sqlite"))
    reset_settings_cache()
    try:
        assert health_summary()["status"] == "unknown"
    finally:
        reset_settings_cache()


def test_health_ok_degraded_stale(db: Path, monkeypatch):
    started = at("2026-09-09T12:00:00")
    monkeypatch.setattr(watchdog, "PROCESS_STARTED", started)

    # Fresh boot, no heartbeat yet: ok (the watchdog has not had its turn).
    s = health_summary(now=started + timedelta(minutes=5))
    assert s == {"status": "ok", "open": [], "checked_utc": None}

    # An hour in with no heartbeat: the scheduler thread is dead.
    assert health_summary(now=started + timedelta(hours=1))["status"] == "stale"

    with connect(db) as conn:
        repo.start_run(conn, WATCHDOG_JOB_ID, started_utc="2026-09-09T12:45:00Z")
        repo.finish_run(conn, WATCHDOG_JOB_ID, "2026-09-09T12:45:00Z", status="ok")
    s = health_summary(now=started + timedelta(hours=1))
    assert s["status"] == "ok"
    assert s["checked_utc"] == "2026-09-09T12:45:00Z"
    assert health_summary(now=started + timedelta(hours=2))["status"] == "stale"

    with connect(db) as conn:
        repo.upsert_problem(
            conn,
            key="brief_daily:missed",
            job_id="brief_daily",
            kind="missed",
            note="n",
            now_utc="2026-09-09T16:00:00Z",
            notified_utc="2026-09-09T16:00:00Z",
        )
    s = health_summary(now=started + timedelta(hours=1))
    assert s["status"] == "degraded"
    assert s["open"] == ["brief_daily:missed"]
