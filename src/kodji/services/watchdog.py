"""Job-missed watchdog (PR-AB, part 2).

Three failure modes an uptime monitor on `/health` cannot see:

- **missed** — a job's cron minute came and went with no run recorded.
  The process was restarted across it (APScheduler's memory job store
  forgets a fire time the moment the process dies), the executor was
  wedged, or the scheduler thread itself is dead.
- **failed** — the job ran and raised. Until now that was one
  `log.exception` line in the journal, read by nobody.
- **stuck** — a run started and never finished. Either it is hung (and
  `max_instances=1` means every later fire is silently skipped), or the
  process was killed mid-run (OOM under `MemoryMax`).

How it works. Every scheduled job body is wrapped by `tracked`, which
writes a row to `job_runs` at start and finish. The watchdog job runs
every 15 minutes, asks each job's *own trigger* for its most recent due
times, and compares them with the table — so a new job added to the
scheduler is covered with no second list to keep in sync. Problems are
persisted in `job_problems` so the operator hears about each one once
when it appears, once a day while it lasts, and once when it clears.

Notification goes to the Discord webhook and/or `OPS_ALERT_EMAIL`,
whichever is configured; with neither, the alert is an ERROR log line.
`/health` exposes a summary (`jobs.status`) so the external uptime
monitor can keyword-match it too.

Thresholds are deliberately constants, not settings: they encode what
the jobs *are* (an OCR sweep can legitimately run for hours, a 5-minute
delivery tick cannot), and an operator who needs to change one should
be reading this file.
"""

from __future__ import annotations

import functools
import html
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import httpx
from apscheduler.triggers.base import BaseTrigger

from kodji.clock import utc_iso, utcnow
from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.services.mailer import EmailMessage, get_mailer
from kodji.store import job_runs as runs_repo

log = get(__name__)

WATCHDOG_JOB_ID = "job_watchdog"

# How long after a due time we wait before calling the run missed. Covers
# scheduler wake-up latency and the one-second cron resolution; a
# long-running job is not "missed", its 'running' row is written at start.
MISS_GRACE = timedelta(minutes=10)
# A run may start this much before its nominal due time and still count
# for it (clock skew between the cron evaluation and `utc_iso()`).
START_SLACK = timedelta(seconds=60)
# Jobs firing at least this often are "frequent": one blip (a deploy
# restart across a 10-minute snapshot, one scraper timeout) is noise, so
# they must miss or fail this many consecutive times before we alert.
FREQUENT_CADENCE = timedelta(hours=1)
FREQUENT_STRIKES = 3
# Longest a run may stay 'running' before it is reported stuck.
DEFAULT_MAX_RUNTIME = timedelta(minutes=30)
MAX_RUNTIME: dict[str, timedelta] = {
    "filings_ocr_daily": timedelta(hours=4),  # ocr_max_files_per_run x ocr_timeout_s
    "filings_pull_daily": timedelta(hours=2),  # every issuer, 0.5 s per PDF
    "fundamentals_extract_daily": timedelta(hours=2),
    "analyst_notes_weekly": timedelta(hours=2),  # 47 Sonnet calls with pauses
    "history_backfill_weekly": timedelta(hours=1),
}
# Widest window we look back for a due time; weekly jobs need > 7 days.
LOOKBACK_WINDOWS = (timedelta(hours=2), timedelta(days=1), timedelta(days=8))
# `job_runs` retention. Long enough to answer "did the brief run last
# Tuesday?", short enough that the table never matters on a 4 GB box.
RETENTION = timedelta(days=30)
# `/health` reports the scheduler stale when the watchdog's own heartbeat
# is older than this — three missed 15-minute ticks.
STALE_AFTER = timedelta(minutes=45)

# When this process came up. A 'running' row older than this belongs to a
# previous process and is closed as interrupted on the first pass.
PROCESS_STARTED = utcnow()


# --- models -----------------------------------------------------------------


@dataclass(frozen=True)
class JobSpec:
    """What the watchdog needs to know about a scheduled job: its id and
    the trigger it can ask for due times."""

    id: str
    trigger: BaseTrigger


@dataclass(frozen=True)
class LastRun:
    job_id: str
    started: datetime
    finished: datetime | None
    status: str
    note: str

    @classmethod
    def from_row(cls, row) -> LastRun:
        return cls(
            job_id=row["job_id"],
            started=_parse(row["started_utc"]),
            finished=_parse(row["finished_utc"]) if row["finished_utc"] else None,
            status=row["status"],
            note=row["note"] or "",
        )


@dataclass(frozen=True)
class Problem:
    job_id: str
    kind: str  # missed | failed | stuck
    note: str

    @property
    def key(self) -> str:
        return f"{self.job_id}:{self.kind}"


class Skipped:
    """Return this from a job body to record the run as 'skipped' (a
    deliberate no-op, like the brief on a public holiday) rather than
    'ok' — so the status table says why nothing was produced."""

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Skipped({self.reason!r})"


@dataclass
class CheckOutcome:
    problems: list[Problem] = field(default_factory=list)
    new: list[Problem] = field(default_factory=list)
    repeated: list[Problem] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    notified: bool | None = None  # None: nothing to send this pass
    orphans_closed: int = 0
    pruned: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "open": len(self.problems),
            "new": [p.key for p in self.new],
            "repeated": [p.key for p in self.repeated],
            "cleared": self.cleared,
            "notified": self.notified,
            "orphans_closed": self.orphans_closed,
            "pruned": self.pruned,
        }


# --- run tracking -----------------------------------------------------------


def _db_path() -> Path:
    return Path(settings.db_path)


def _parse(stamp: str) -> datetime:
    dt = datetime.fromisoformat(stamp)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summary(result: object) -> str:
    as_dict = getattr(result, "as_dict", None)
    return str(as_dict() if callable(as_dict) else result)


def _record_start(job_id: str) -> str:
    started = utc_iso()
    try:
        with connect(_db_path()) as conn:
            runs_repo.start_run(conn, job_id, started_utc=started)
    except Exception as e:  # pragma: no cover - the job must run even if bookkeeping fails
        log.warning("watchdog: could not record start of %s: %s", job_id, e)
    return started


def _record_finish(job_id: str, started: str, status: str, note: str) -> None:
    try:
        with connect(_db_path()) as conn:
            runs_repo.finish_run(conn, job_id, started, status=status, note=note)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("watchdog: could not record finish of %s: %s", job_id, e)


def tracked(job_id: str, fn: Callable[[], object]) -> Callable[[], None]:
    """Wrap a job body so every run lands in `job_runs`.

    The wrapper owns the start/ok/failed logging the bodies used to
    repeat by hand, and it swallows exceptions: a job must never kill
    the scheduler, and the failure is now recorded where the watchdog
    will see it rather than only in the journal.
    """

    @functools.wraps(fn)
    def run() -> None:
        started = _record_start(job_id)
        log.info("scheduled %s start", job_id)
        try:
            result = fn()
        except Exception as e:
            log.exception("scheduled %s failed: %s", job_id, e)
            _record_finish(job_id, started, "failed", _short(f"{type(e).__name__}: {e}"))
            return
        if isinstance(result, Skipped):
            log.info("scheduled %s skipped: %s", job_id, result.reason)
            _record_finish(job_id, started, "skipped", _short(result.reason))
            return
        summary = _summary(result)
        log.info("scheduled %s ok: %s", job_id, summary)
        _record_finish(job_id, started, "ok", _short(summary))

    return run


# --- the check (pure) -------------------------------------------------------


def last_due_times(trigger: BaseTrigger, limit: datetime, count: int = 1) -> list[datetime]:
    """The `count` most recent fire times at or before `limit`, oldest
    first — or fewer if the trigger has not fired that often within the
    widest lookback, and none if it has never been due in it.

    APScheduler triggers only walk forward, so we start a window back and
    step to the last fire time under `limit`, widening the window until
    it holds enough fire times. A 5-minute job resolves inside the 2-hour
    window; a weekly job needs the 8-day one.
    """
    due: list[datetime] = []
    for window in LOOKBACK_WINDOWS:
        t = trigger.get_next_fire_time(None, limit - window)
        due = []
        while t is not None and t <= limit:
            due.append(t)
            t = trigger.get_next_fire_time(t, t)
        if len(due) >= count:
            break
    return due[-count:]


def max_runtime(job_id: str) -> timedelta:
    return MAX_RUNTIME.get(job_id, DEFAULT_MAX_RUNTIME)


def _fmt_delta(d: timedelta) -> str:
    total = int(d.total_seconds())
    h, rem = divmod(total, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def check(
    jobs: Sequence[JobSpec],
    runs: dict[str, LastRun],
    *,
    now: datetime,
    watch_since: datetime | None,
    streaks: Callable[[str], int] | None = None,
) -> list[Problem]:
    """Compare every job's recent due times with its last recorded run.

    `runs` is the latest run per job id; `streaks(job_id)` returns the
    current consecutive-failure count and is only consulted for a job
    whose last run failed. `watch_since` is when tracking began — a due
    time before it is unjudgeable and skipped. Pure apart from those two
    inputs, so the tests drive it with real CronTriggers and a frozen
    clock.
    """
    problems: list[Problem] = []
    for job in jobs:
        if job.id == WATCHDOG_JOB_ID:
            continue
        due = last_due_times(job.trigger, now - MISS_GRACE, FREQUENT_STRIKES)
        if not due:
            continue
        gaps = [b - a for a, b in pairwise(due)]
        frequent = bool(gaps) and min(gaps) <= FREQUENT_CADENCE
        strikes = FREQUENT_STRIKES if frequent else 1
        required = due[-strikes:]
        threshold = required[0]
        if watch_since is None or threshold < watch_since:
            continue

        last = runs.get(job.id)
        if last is not None and last.status == "running":
            runtime = now - last.started
            if runtime > max_runtime(job.id):
                problems.append(
                    Problem(
                        job.id,
                        "stuck",
                        f"started {utc_iso(last.started)}, still running after {_fmt_delta(runtime)}",
                    )
                )
            continue

        if last is None or last.started < threshold - START_SLACK:
            when = "never" if last is None else f"{utc_iso(last.started)} ({last.status})"
            missed = len(required) if frequent else 1
            note = f"due {utc_iso(due[-1])}, last run {when}"
            if missed > 1:
                note = f"{missed} consecutive due times, " + note
            problems.append(Problem(job.id, "missed", note))
            continue

        if last.status == "failed":
            streak = streaks(job.id) if streaks else 1
            if streak >= strikes:
                problems.append(
                    Problem(job.id, "failed", f"{streak} consecutive failure(s); last: {last.note}")
                )
    return problems


# --- the check (against the DB) --------------------------------------------


def _scan(conn, jobs: Sequence[JobSpec], now: datetime) -> list[Problem]:
    runs = {job_id: LastRun.from_row(row) for job_id, row in runs_repo.latest_runs(conn).items()}
    since_stamp = runs_repo.watch_since(conn)
    since = _parse(since_stamp) if since_stamp else None
    return check(
        jobs,
        runs,
        now=now,
        watch_since=since,
        streaks=lambda job_id: runs_repo.failure_streak(conn, job_id),
    )


def check_now(jobs: Sequence[JobSpec], *, now: datetime | None = None) -> list[Problem]:
    """Read-only: what the watchdog would report right now. For the CLI
    — it does not close orphans, notify, or touch `job_problems`, so it
    is safe to run beside the live process."""
    with connect(_db_path()) as conn:
        return _scan(conn, jobs, now or utcnow())


def run_check(
    jobs: Sequence[JobSpec],
    *,
    now: datetime | None = None,
    send: Callable[[str, str], bool] | None = None,
) -> CheckOutcome:
    """One watchdog pass: close orphaned runs, scan, reconcile with the
    open problems, notify about what changed, prune old rows.

    `send(subject, text)` returns whether at least one channel accepted
    the message; tests inject one, production uses `send_ops_alert`.
    """
    now = now or utcnow()
    send = send or send_ops_alert
    out = CheckOutcome()
    repeat_after = timedelta(hours=settings.ops_alert_repeat_hours)

    with connect(_db_path()) as conn:
        out.orphans_closed = runs_repo.fail_orphans(
            conn, before_utc=utc_iso(PROCESS_STARTED), finished_utc=utc_iso(now)
        )
        out.problems = _scan(conn, jobs, now)

        open_rows = {r["key"]: r for r in runs_repo.open_problems(conn)}
        current = {p.key: p for p in out.problems}
        out.new = [p for k, p in current.items() if k not in open_rows]
        out.cleared = [k for k in open_rows if k not in current]
        out.repeated = [
            p
            for k, p in current.items()
            if k in open_rows and now - _parse(open_rows[k]["last_notified_utc"]) >= repeat_after
        ]

        if out.new or out.cleared or out.repeated:
            out.notified = send(*_compose(out, now))
        # A failed send leaves `last_notified_utc` in the past so the next
        # pass treats the problem as due for a repeat and tries again.
        stamp = utc_iso(now) if out.notified else utc_iso(now - repeat_after)
        for p in out.new:
            runs_repo.upsert_problem(
                conn,
                key=p.key,
                job_id=p.job_id,
                kind=p.kind,
                note=p.note,
                now_utc=utc_iso(now),
                notified_utc=stamp,
            )
        for p in out.repeated:
            runs_repo.touch_problem(conn, p.key, note=p.note, notified_utc=stamp)
        for k in out.cleared:
            runs_repo.clear_problem(conn, k)

        out.pruned = runs_repo.prune(conn, before_utc=utc_iso(now - RETENTION))
    return out


# --- notification -----------------------------------------------------------


def _compose(out: CheckOutcome, now: datetime) -> tuple[str, str]:
    parts = []
    if out.new:
        parts.append(f"{len(out.new)} new")
    if out.cleared:
        parts.append(f"{len(out.cleared)} cleared")
    if out.repeated:
        parts.append(f"{len(out.repeated)} still open")
    subject = f"kodji watchdog: {', '.join(parts)}"
    lines = [f"{utc_iso(now)} — scheduled-job check"]
    lines += [f"NEW    {p.job_id} {p.kind} — {p.note}" for p in out.new]
    lines += [f"CLEAR  {k.replace(':', ' ', 1)} — recovered" for k in out.cleared]
    lines += [f"STILL  {p.job_id} {p.kind} — {p.note}" for p in out.repeated]
    lines.append(f"open problems now: {len(out.problems)}")
    if settings.public_base_url:
        lines.append(f"{settings.public_base_url.rstrip('/')}/health")
    return subject, "\n".join(lines)


def _post_discord(subject: str, text: str) -> bool:
    body = f"**{subject}**\n```\n{text[:1800]}\n```"
    try:
        with httpx.Client(timeout=settings.http_timeout_s) as client:
            resp = client.post(
                settings.discord_webhook_url,
                json={"content": body, "username": "kodji-watchdog"},
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        # Never log the URL: httpx puts it in the exception text.
        log.warning("watchdog: discord post failed: %s", type(e).__name__)
        return False
    return True


def _send_email(subject: str, text: str) -> bool:
    mailer = get_mailer()
    try:
        result = mailer.send(
            EmailMessage(
                to=settings.ops_alert_email,
                subject=subject,
                text=text,
                html=f"<pre>{html.escape(text)}</pre>",
            )
        )
    finally:
        mailer.close()
    if not result.ok:
        log.warning("watchdog: email failed: %s", result.note)
    return result.ok


def send_ops_alert(subject: str, text: str) -> bool:
    """Deliver to every configured channel.

    True when at least one accepted it. With no channel configured the
    ERROR log line *is* the channel and counts as delivered — otherwise
    the watchdog would retry every pass forever.
    """
    attempts: list[bool] = []
    if settings.has_discord:
        attempts.append(_post_discord(subject, text))
    if settings.ops_alert_email:
        attempts.append(_send_email(subject, text))
    if not attempts:
        log.error(
            "%s\n%s\n(set DISCORD_WEBHOOK_URL or OPS_ALERT_EMAIL to get this off the log)",
            subject,
            text,
        )
        return True
    return any(attempts)


# --- /health ----------------------------------------------------------------


def health_summary(now: datetime | None = None) -> dict[str, object]:
    """The `jobs` block of `/health`.

    `degraded` — at least one problem is open; `stale` — the watchdog's
    own heartbeat is missing (the scheduler thread is dead while uvicorn
    keeps answering); `unknown` — the DB is unreadable from here. Only
    problem keys are exposed: the endpoint is public, and failure notes
    can carry exception text.
    """
    now = now or utcnow()
    db = _db_path()
    if not db.exists():
        return {"status": "unknown", "open": [], "checked_utc": None}
    try:
        with connect(db) as conn:
            keys = runs_repo.open_problem_keys(conn)
            beat = runs_repo.latest_run(conn, WATCHDOG_JOB_ID)
    except Exception as e:
        log.warning("watchdog: health read failed: %s", e)
        return {"status": "unknown", "open": [], "checked_utc": None}

    checked = beat["started_utc"] if beat else None
    if keys:
        status = "degraded"
    elif now - PROCESS_STARTED > STALE_AFTER and (
        checked is None or now - _parse(checked) > STALE_AFTER
    ):
        status = "stale"
    else:
        status = "ok"
    return {"status": status, "open": keys, "checked_utc": checked}


# --- CLI support ------------------------------------------------------------


@dataclass(frozen=True)
class StatusRow:
    job_id: str
    next_due: datetime | None
    last: LastRun | None

    @property
    def duration(self) -> timedelta | None:
        if self.last is None or self.last.finished is None:
            return None
        return self.last.finished - self.last.started


def status_rows(jobs: Sequence[JobSpec], *, now: datetime | None = None) -> list[StatusRow]:
    now = now or utcnow()
    with connect(_db_path()) as conn:
        runs = {
            job_id: LastRun.from_row(row) for job_id, row in runs_repo.latest_runs(conn).items()
        }
    return [
        StatusRow(
            job_id=job.id,
            next_due=job.trigger.get_next_fire_time(None, now),
            last=runs.get(job.id),
        )
        for job in jobs
    ]
