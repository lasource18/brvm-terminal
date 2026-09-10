"""Tests for store/job_runs.py: the scheduled-job audit trail and the
watchdog's open-problem set."""

from __future__ import annotations

from pathlib import Path

from kodji.db import connect
from kodji.store import job_runs as repo

from .conftest import apply_migrations


def _init(tmp_db_path: Path) -> None:
    with connect(tmp_db_path) as conn:
        apply_migrations(conn)


def test_start_then_finish_round_trips(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        started = repo.start_run(conn, "brief_daily", started_utc="2026-09-09T15:45:01Z")
        assert started == "2026-09-09T15:45:01Z"
        row = repo.latest_run(conn, "brief_daily")
        assert row["status"] == "running"
        assert row["finished_utc"] is None

        repo.finish_run(
            conn,
            "brief_daily",
            started,
            status="ok",
            note="{'n': 3}",
            finished_utc="2026-09-09T15:46:10Z",
        )
        row = repo.latest_run(conn, "brief_daily")
        assert row["status"] == "ok"
        assert row["note"] == "{'n': 3}"
        assert row["finished_utc"] == "2026-09-09T15:46:10Z"


def test_latest_runs_picks_newest_row_per_job(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        for stamp, status in (("2026-09-08T15:45:00Z", "ok"), ("2026-09-09T15:45:00Z", "failed")):
            repo.start_run(conn, "brief_daily", started_utc=stamp)
            repo.finish_run(conn, "brief_daily", stamp, status=status)
        repo.start_run(conn, "news_hourly_outside", started_utc="2026-09-09T16:23:00Z")

        latest = repo.latest_runs(conn)
        assert set(latest) == {"brief_daily", "news_hourly_outside"}
        assert latest["brief_daily"]["started_utc"] == "2026-09-09T15:45:00Z"
        assert latest["brief_daily"]["status"] == "failed"
        assert latest["news_hourly_outside"]["status"] == "running"

        history = repo.recent_runs(conn, "brief_daily")
        assert [r["started_utc"] for r in history] == [
            "2026-09-09T15:45:00Z",
            "2026-09-08T15:45:00Z",
        ]


def test_failure_streak_counts_only_the_tail(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        assert repo.failure_streak(conn, "news_hourly_outside") == 0
        seq = [
            ("2026-09-09T10:23:00Z", "failed"),
            ("2026-09-09T11:23:00Z", "ok"),
            ("2026-09-09T12:23:00Z", "failed"),
            ("2026-09-09T13:23:00Z", "failed"),
        ]
        for stamp, status in seq:
            repo.start_run(conn, "news_hourly_outside", started_utc=stamp)
            repo.finish_run(conn, "news_hourly_outside", stamp, status=status)
        assert repo.failure_streak(conn, "news_hourly_outside") == 2

        # A job that has only ever failed counts every failure.
        repo.start_run(conn, "boc_reconcile_daily", started_utc="2026-09-09T16:00:00Z")
        repo.finish_run(conn, "boc_reconcile_daily", "2026-09-09T16:00:00Z", status="failed")
        assert repo.failure_streak(conn, "boc_reconcile_daily") == 1


def test_watch_since_is_the_oldest_row(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        assert repo.watch_since(conn) is None
        repo.start_run(conn, "a", started_utc="2026-09-05T00:00:00Z")
        repo.start_run(conn, "b", started_utc="2026-09-01T00:00:00Z")
        assert repo.watch_since(conn) == "2026-09-01T00:00:00Z"


def test_fail_orphans_closes_only_running_rows_from_before(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        repo.start_run(conn, "filings_ocr_daily", started_utc="2026-09-08T02:00:00Z")  # orphan
        repo.start_run(conn, "brief_daily", started_utc="2026-09-08T15:45:00Z")
        repo.finish_run(conn, "brief_daily", "2026-09-08T15:45:00Z", status="ok")
        repo.start_run(conn, "news_hourly_outside", started_utc="2026-09-09T10:23:00Z")  # live

        n = repo.fail_orphans(
            conn, before_utc="2026-09-09T09:00:00Z", finished_utc="2026-09-09T09:00:30Z"
        )
        assert n == 1
        ocr = repo.latest_run(conn, "filings_ocr_daily")
        assert ocr["status"] == "failed"
        assert ocr["note"] == repo.INTERRUPTED_NOTE
        assert ocr["finished_utc"] == "2026-09-09T09:00:30Z"
        assert repo.latest_run(conn, "brief_daily")["status"] == "ok"
        assert repo.latest_run(conn, "news_hourly_outside")["status"] == "running"


def test_prune_drops_old_rows(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        repo.start_run(conn, "a", started_utc="2026-08-01T00:00:00Z")
        repo.start_run(conn, "a", started_utc="2026-09-01T00:00:00Z")
        assert repo.prune(conn, before_utc="2026-08-15T00:00:00Z") == 1
        assert repo.watch_since(conn) == "2026-09-01T00:00:00Z"


def test_problems_upsert_touch_clear(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        assert repo.open_problems(conn) == []
        repo.upsert_problem(
            conn,
            key="brief_daily:missed",
            job_id="brief_daily",
            kind="missed",
            note="due 15:45",
            now_utc="2026-09-09T16:00:00Z",
            notified_utc="2026-09-09T16:00:00Z",
        )
        # Re-upsert keeps first_seen_utc but refreshes note + last_notified.
        repo.upsert_problem(
            conn,
            key="brief_daily:missed",
            job_id="brief_daily",
            kind="missed",
            note="due 15:45 again",
            now_utc="2026-09-10T16:00:00Z",
            notified_utc="2026-09-10T16:00:00Z",
        )
        rows = repo.open_problems(conn)
        assert len(rows) == 1
        assert rows[0]["first_seen_utc"] == "2026-09-09T16:00:00Z"
        assert rows[0]["last_notified_utc"] == "2026-09-10T16:00:00Z"
        assert rows[0]["note"] == "due 15:45 again"
        assert repo.open_problem_keys(conn) == ["brief_daily:missed"]

        repo.touch_problem(
            conn, "brief_daily:missed", note="n3", notified_utc="2026-09-11T16:00:00Z"
        )
        assert repo.open_problems(conn)[0]["last_notified_utc"] == "2026-09-11T16:00:00Z"

        assert repo.clear_problem(conn, "brief_daily:missed") == 1
        assert repo.clear_problem(conn, "brief_daily:missed") == 0
        assert repo.open_problems(conn) == []
