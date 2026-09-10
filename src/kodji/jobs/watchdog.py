"""Read-only view of the scheduled jobs for `just jobs-status` / `just jobs-check`.

Builds the scheduler without starting it — that is the only way to get
the real job list and triggers — then reads `job_runs`. Neither command
notifies, closes orphans, or touches `job_problems`; the live process's
watchdog owns those, and this runs beside it.
"""

from __future__ import annotations

import argparse
import logging
import sys

from kodji.clock import utc_iso, utcnow
from kodji.jobs.scheduler import build_scheduler
from kodji.services.watchdog import (
    WATCHDOG_JOB_ID,
    JobSpec,
    check_now,
    status_rows,
)


def _specs() -> list[JobSpec]:
    # Building the scheduler logs one "Adding job tentatively" line per
    # job at INFO; that is noise in a status listing.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    sched = build_scheduler()
    return [JobSpec(j.id, j.trigger) for j in sched.get_jobs()]


def print_status() -> None:
    now = utcnow()
    rows = status_rows(_specs(), now=now)
    print(f"\nscheduled jobs at {utc_iso(now)}\n")
    print(
        f"  {'job':<32} {'next due (UTC)':<21} {'last run (UTC)':<21} {'status':<8} {'took':>7}  note"
    )
    for r in rows:
        nxt = utc_iso(r.next_due)[:19] if r.next_due else "-"
        if r.last is None:
            last, status, took, note = "never", "-", "-", ""
        else:
            last = utc_iso(r.last.started)[:19]
            status = r.last.status
            d = r.duration
            took = f"{int(d.total_seconds())}s" if d is not None else "…"
            note = r.last.note[:60]
        print(f"  {r.job_id:<32} {nxt:<21} {last:<21} {status:<8} {took:>7}  {note}")
    print()


def print_check() -> int:
    now = utcnow()
    problems = check_now([s for s in _specs() if s.id != WATCHDOG_JOB_ID], now=now)
    print(f"\nwatchdog check at {utc_iso(now)}: {len(problems)} problem(s)\n")
    for p in problems:
        print(f"  {p.kind:<7} {p.job_id:<32} {p.note}")
    if problems:
        print()
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="kodji-terminal scheduled-job status")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what the watchdog would flag right now (exit 1 if anything)",
    )
    args = parser.parse_args(argv)
    if args.check:
        return print_check()
    print_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
