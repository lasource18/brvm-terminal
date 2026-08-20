"""Time + market-hours helpers.

BRVM is quoted in Africa/Abidjan (UTC+0, no DST). Continuous trading runs
Mon-Fri ~09:00-15:00 local. Session date == Abidjan calendar date.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

ABIDJAN = ZoneInfo("Africa/Abidjan")

MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 0)


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def now_abidjan() -> datetime:
    return datetime.now(tz=ABIDJAN)


def to_abidjan(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ABIDJAN)


def session_date_for(dt: datetime | None = None) -> date:
    return to_abidjan(dt or utcnow()).date()


def is_market_open(dt: datetime | None = None) -> bool:
    local = to_abidjan(dt or utcnow())
    if local.weekday() >= 5:  # Sat/Sun
        return False
    t = local.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def utc_iso(dt: datetime | None = None) -> str:
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
