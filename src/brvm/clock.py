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


def last_completed_session_date(dt: datetime | None = None) -> date:
    """Return the Abidjan calendar date of the most recent trading
    session that has *actually happened* by `dt`.

    Used by ingest paths that pull a source displaying the last-traded
    level (sikafinance's A-to-Z page, afx.kwayisi's daily summary) and
    need to stamp `session_date` correctly for weekend / holiday /
    pre-open polls. Without this, a Sunday poll would stamp Sunday and
    accumulate a phantom weekend row on top of Friday's real one
    (audit F-11).

    Rules:
      - Weekday, at/after 09:00 local: today.
      - Weekday, before 09:00 local: the previous weekday (Fri if
        today is Monday).
      - Weekend: the previous Friday.

    Holidays are not modelled here — the WAEMU public-holiday calendar
    isn't in the codebase yet, so a Monday-holiday poll will still
    stamp Monday. Better to fix that when we import a holiday source.
    """
    local = to_abidjan(dt or utcnow())
    today = local.date()
    # Weekend rollback: back up to the previous Friday.
    if today.weekday() >= 5:
        offset = today.weekday() - 4  # Sat → 1, Sun → 2
        return date.fromordinal(today.toordinal() - offset)
    # Weekday before market open: back up one weekday.
    if local.time() < MARKET_OPEN:
        # Monday before open: back up to Friday (3 days), else 1 day.
        offset = 3 if today.weekday() == 0 else 1
        return date.fromordinal(today.toordinal() - offset)
    return today


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
