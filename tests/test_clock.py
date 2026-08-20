from datetime import UTC, datetime

from freezegun import freeze_time

from brvm.clock import is_market_open, session_date_for, to_abidjan, utc_iso


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
