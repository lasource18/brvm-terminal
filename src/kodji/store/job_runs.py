"""SQLite repository for scheduled-job runs and the watchdog's open problems.

`job_runs` is append-mostly: one row per (job_id, started_utc), stamped
'running' at start and finished with 'ok' / 'skipped' / 'failed'. The
watchdog (services/watchdog.py) reads the latest row per job; the CLI
reads a short history. Rows older than the retention window are pruned
by the watchdog itself.

`job_problems` holds the problems currently open so the watchdog can
tell "new" from "still there" from "cleared" across its 15-minute passes.

Timestamps are `clock.utc_iso()` strings (second precision, trailing Z),
which sort lexicographically in time order — every query here relies on
that.
"""

from __future__ import annotations

import sqlite3

from kodji.clock import utc_iso

INTERRUPTED_NOTE = "interrupted: process restarted mid-run"


# --- job_runs ---------------------------------------------------------------


def start_run(conn: sqlite3.Connection, job_id: str, *, started_utc: str | None = None) -> str:
    """Insert the 'running' row. Returns the start stamp the caller must
    hand back to `finish_run`."""
    started = started_utc or utc_iso()
    conn.execute(
        """
        INSERT OR REPLACE INTO job_runs (job_id, started_utc, finished_utc, status, note)
        VALUES (?, ?, NULL, 'running', '')
        """,
        (job_id, started),
    )
    conn.commit()
    return started


def finish_run(
    conn: sqlite3.Connection,
    job_id: str,
    started_utc: str,
    *,
    status: str,
    note: str = "",
    finished_utc: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE job_runs SET finished_utc = ?, status = ?, note = ?
        WHERE job_id = ? AND started_utc = ?
        """,
        (finished_utc or utc_iso(), status, note, job_id, started_utc),
    )
    conn.commit()


def latest_run(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM job_runs WHERE job_id = ? ORDER BY started_utc DESC LIMIT 1",
        (job_id,),
    ).fetchone()


def latest_runs(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """The most recent row of every job that has ever run, keyed by id."""
    rows = conn.execute(
        """
        SELECT r.* FROM job_runs r
        WHERE r.started_utc = (
            SELECT MAX(started_utc) FROM job_runs r2 WHERE r2.job_id = r.job_id
        )
        """
    ).fetchall()
    return {row["job_id"]: row for row in rows}


def recent_runs(conn: sqlite3.Connection, job_id: str, *, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM job_runs WHERE job_id = ? ORDER BY started_utc DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
    )


def failure_streak(conn: sqlite3.Connection, job_id: str) -> int:
    """How many 'failed' rows sit after the job's last non-failed run.

    A job that has only ever failed counts every failure; a job that has
    never run counts zero.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) FROM job_runs
        WHERE job_id = ? AND status = 'failed'
          AND started_utc > COALESCE(
              (SELECT MAX(started_utc) FROM job_runs
               WHERE job_id = ? AND status != 'failed'),
              '')
        """,
        (job_id, job_id),
    ).fetchone()
    return int(row[0]) if row else 0


def watch_since(conn: sqlite3.Connection) -> str | None:
    """When run tracking began: the oldest row still in the table. A due
    time before this cannot be judged, so the watchdog skips it."""
    row = conn.execute("SELECT MIN(started_utc) FROM job_runs").fetchone()
    return row[0] if row and row[0] else None


def fail_orphans(
    conn: sqlite3.Connection, *, before_utc: str, finished_utc: str | None = None
) -> int:
    """Close 'running' rows that started before `before_utc`.

    The scheduler lives in a single process, so a row still 'running'
    from before this process started belongs to a process that is gone —
    killed mid-job (OOM, deploy restart). Marking it failed lets the
    watchdog report the interruption instead of showing a job that has
    been "running" for a week.
    """
    cur = conn.execute(
        """
        UPDATE job_runs SET status = 'failed', finished_utc = ?, note = ?
        WHERE status = 'running' AND started_utc < ?
        """,
        (finished_utc or utc_iso(), INTERRUPTED_NOTE, before_utc),
    )
    conn.commit()
    return cur.rowcount


def prune(conn: sqlite3.Connection, *, before_utc: str) -> int:
    cur = conn.execute("DELETE FROM job_runs WHERE started_utc < ?", (before_utc,))
    conn.commit()
    return cur.rowcount


# --- job_problems -----------------------------------------------------------


def open_problems(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM job_problems ORDER BY first_seen_utc, key").fetchall())


def open_problem_keys(conn: sqlite3.Connection) -> list[str]:
    return [r["key"] for r in conn.execute("SELECT key FROM job_problems ORDER BY key").fetchall()]


def upsert_problem(
    conn: sqlite3.Connection,
    *,
    key: str,
    job_id: str,
    kind: str,
    note: str,
    now_utc: str,
    notified_utc: str,
) -> None:
    """Insert a newly-seen problem, or refresh the note of one already
    open without touching `first_seen_utc`."""
    conn.execute(
        """
        INSERT INTO job_problems (key, job_id, kind, note, first_seen_utc, last_notified_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            note = excluded.note,
            last_notified_utc = excluded.last_notified_utc
        """,
        (key, job_id, kind, note, now_utc, notified_utc),
    )
    conn.commit()


def touch_problem(conn: sqlite3.Connection, key: str, *, note: str, notified_utc: str) -> None:
    conn.execute(
        "UPDATE job_problems SET note = ?, last_notified_utc = ? WHERE key = ?",
        (note, notified_utc, key),
    )
    conn.commit()


def clear_problem(conn: sqlite3.Connection, key: str) -> int:
    cur = conn.execute("DELETE FROM job_problems WHERE key = ?", (key,))
    conn.commit()
    return cur.rowcount
