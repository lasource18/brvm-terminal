"""Tests for store/spend.py: the daily LLM budget counter."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from kodji.db import connect
from kodji.store import spend as spend_repo

from .conftest import apply_migrations

DAY = date(2026, 8, 21)


def _init(tmp_db_path: Path) -> None:
    with connect(tmp_db_path) as conn:
        apply_migrations(conn)


def test_empty_day_has_no_spend_and_full_headroom(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        assert spend_repo.spent_micros(conn, DAY) == 0
        assert spend_repo.remaining_micros(conn, 100, DAY) == 1_000_000  # $1


def test_usage_accumulates_across_calls(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        spend_repo.add_usage(conn, input_tokens=1000, output_tokens=400, usd_micros=3000, day=DAY)
        total = spend_repo.add_usage(
            conn, input_tokens=500, output_tokens=200, usd_micros=1500, day=DAY
        )
        assert total == 4500
        row = spend_repo.get_day(conn, DAY)
        assert row["calls"] == 2
        assert row["input_tokens"] == 1500
        assert row["output_tokens"] == 600
        assert row["usd_micros"] == 4500


def test_sub_cent_calls_still_accumulate(tmp_db_path: Path):
    """Whole-cent accounting would round each of these to zero forever."""
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        for _ in range(400):
            spend_repo.add_usage(conn, input_tokens=1, output_tokens=1, usd_micros=3000, day=DAY)
        assert spend_repo.spent_micros(conn, DAY) == 1_200_000
        # usd_cents mirror stays readable for a human at the sqlite3 prompt.
        assert spend_repo.get_day(conn, DAY)["usd_cents"] == 120


def test_remaining_floors_at_zero_once_the_cap_is_blown(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        spend_repo.add_usage(conn, input_tokens=0, output_tokens=0, usd_micros=1_500_000, day=DAY)
        assert spend_repo.remaining_micros(conn, 100, DAY) == 0


def test_days_are_independent(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        spend_repo.add_usage(conn, input_tokens=0, output_tokens=0, usd_micros=1_000_000, day=DAY)
        assert spend_repo.remaining_micros(conn, 100, DAY) == 0
        assert spend_repo.remaining_micros(conn, 100, date(2026, 8, 22)) == 1_000_000
        assert len(spend_repo.recent(conn)) == 1
