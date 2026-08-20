from freezegun import freeze_time

from brvm.clock import is_market_open


@freeze_time("2026-08-19 12:00:00", tz_offset=0)  # Wed midday
def test_open_on_weekday_midday():
    assert is_market_open() is True


@freeze_time("2026-08-19 07:30:00", tz_offset=0)  # Wed 07:30 UTC == Abidjan (before open)
def test_closed_before_open():
    assert is_market_open() is False


@freeze_time("2026-08-19 15:30:00", tz_offset=0)  # Wed after close
def test_closed_after_close():
    assert is_market_open() is False


@freeze_time("2026-08-15 12:00:00", tz_offset=0)  # Saturday
def test_closed_on_saturday():
    assert is_market_open() is False


def test_scheduler_builds():
    # Import late so freezegun doesn't apply to module import (not needed here).
    from brvm.jobs.scheduler import build_scheduler

    sched = build_scheduler()
    ids = {j.id for j in sched.get_jobs()}
    assert "snapshot_market_hours" in ids
    assert "snapshot_hourly_outside" in ids
    assert "news_market_hours" in ids
    assert "news_hourly_outside" in ids
