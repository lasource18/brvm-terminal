"""Time + market-hours helpers.

BRVM is quoted in Africa/Abidjan (UTC+0, no DST). Continuous trading runs
Mon-Fri ~09:00-15:00 local. Session date == Abidjan calendar date.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.easter import easter

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
    if is_market_holiday(local.date()):
        return False
    t = local.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


# F-18: BRVM observes WAEMU-wide public holidays. Full coverage would
# require the Islamic lunar calendar (Eid al-Fitr, Eid al-Adha, Mawlid)
# and country-specific state holidays across 8 members. Scope this to
# the reliably-fixable dates: five civil holidays that are always on
# the same Gregorian date, plus Easter Monday which `dateutil.easter`
# computes exactly. The two or three Islamic holidays per year will
# still produce a stale "session recap" — flagged as a known limitation
# in the phase notes; better than a Mon-Fri cron with zero gating.
_FIXED_HOLIDAYS: tuple[tuple[int, int], ...] = (
    (1, 1),    # New Year's Day
    (5, 1),    # Labour Day
    (8, 15),   # Assumption
    (11, 1),   # All Saints
    (12, 25),  # Christmas
)


def _movable_holidays(year: int) -> set[date]:
    """Christian holidays computed from the year's Easter Sunday."""
    e = easter(year)
    return {
        e + timedelta(days=1),   # Easter Monday
        e + timedelta(days=39),  # Ascension (Thursday, 39 days after Easter)
        e + timedelta(days=50),  # Pentecost Monday (Whit Monday)
    }


def is_market_holiday(d: date) -> bool:
    """True on WAEMU civil / Christian holidays the BRVM is dark for.

    Does NOT cover Islamic holidays (Eid al-Fitr, Eid al-Adha, Mawlid)
    because those move on the lunar calendar and adding a full Islamic
    calendar dep for two or three days a year isn't a good trade.
    """
    if (d.month, d.day) in _FIXED_HOLIDAYS:
        return True
    return d in _movable_holidays(d.year)


def utc_iso(dt: datetime | None = None) -> str:
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
