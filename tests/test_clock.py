from datetime import UTC, date, datetime

from freezegun import freeze_time

from brvm.clock import (
    is_market_open,
    last_completed_session_date,
    session_date_for,
    to_abidjan,
    utc_iso,
)


@freeze_time("2026-08-18 10:30:00", tz_offset=0)  # UTC == Abidjan
def test_market_open_weekday_midday():
    assert is_market_open() is True


@freeze_time("2026-08-18 22:30:00", tz_offset=0)
def test_market_closed_after_hours():
    assert is_market_open() is False


@freeze_time("2026-08-16 12:00:00", tz_offset=0)  # Sunday
def test_market_closed_weekend():
    assert is_market_open() is False


def test_session_date_uses_abidjan():
    dt = datetime(2026, 8, 18, 23, 30, tzinfo=UTC)
    assert session_date_for(dt).isoformat() == "2026-08-18"


def test_to_abidjan_naive_input_treated_as_utc():
    dt = datetime(2026, 8, 18, 12, 0)
    local = to_abidjan(dt)
    assert local.tzinfo is not None
    assert local.hour == 12  # Africa/Abidjan is UTC+0


def test_utc_iso_endswith_z():
    dt = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    assert utc_iso(dt).endswith("Z")


# F-11: last_completed_session_date must roll back correctly on
# weekends and pre-open weekday mornings so ingest paths that call it
# never stamp a Sat/Sun row or a phantom Monday-before-open row.


@freeze_time("2026-08-19 12:00:00", tz_offset=0)  # Wed midday Abidjan
def test_last_completed_session_weekday_after_open_is_today():
    assert last_completed_session_date() == date(2026, 8, 19)


@freeze_time("2026-08-22 12:00:00", tz_offset=0)  # Sat midday
def test_last_completed_session_saturday_rolls_to_friday():
    assert last_completed_session_date() == date(2026, 8, 21)


@freeze_time("2026-08-23 12:00:00", tz_offset=0)  # Sun midday
def test_last_completed_session_sunday_rolls_to_friday():
    assert last_completed_session_date() == date(2026, 8, 21)


@freeze_time("2026-08-24 06:00:00", tz_offset=0)  # Mon 06:00 Abidjan (pre-open)
def test_last_completed_session_monday_preopen_rolls_to_friday():
    assert last_completed_session_date() == date(2026, 8, 21)


@freeze_time("2026-08-20 06:00:00", tz_offset=0)  # Thu 06:00 Abidjan (pre-open)
def test_last_completed_session_weekday_preopen_rolls_back_one_day():
    assert last_completed_session_date() == date(2026, 8, 19)
