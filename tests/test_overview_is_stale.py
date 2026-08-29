"""F-10: `Overview.is_stale` must reflect scraper freshness, not
market status. Pin the four rules explicitly so a regression that
re-inverts the meaning trips a test rather than a stale badge in
production."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from brvm.services._view import Overview


def _at(delta: timedelta) -> str:
    """ISO-8601 timestamp offset from utcnow by `delta`."""
    return (datetime.now(UTC) + delta).isoformat()


def _overview(*, market_open: bool, last_snapshot_utc: str | None) -> Overview:
    return Overview(
        generated_utc=datetime.now(UTC).isoformat(),
        market_open=market_open,
        last_snapshot_utc=last_snapshot_utc,
    )


def test_no_snapshot_is_stale_when_market_open():
    ov = _overview(market_open=True, last_snapshot_utc=None)
    assert ov.is_stale


def test_no_snapshot_is_stale_when_market_closed():
    """Absent snapshot is always stale — the scraper never landed
    anything, which is worse than a lagging one and always worth
    flagging."""
    ov = _overview(market_open=False, last_snapshot_utc=None)
    assert ov.is_stale


def test_fresh_snapshot_during_market_hours_is_not_stale():
    ov = _overview(market_open=True, last_snapshot_utc=_at(-timedelta(minutes=5)))
    assert not ov.is_stale


def test_snapshot_over_threshold_during_market_hours_is_stale():
    """The audit's headline case: scraper dead for six hours during
    trading. Under the old `not market_open` rule this rendered
    without any badge."""
    ov = _overview(market_open=True, last_snapshot_utc=_at(-timedelta(hours=6)))
    assert ov.is_stale


def test_fresh_snapshot_after_market_close_is_not_stale():
    """The audit's inverted case: fresh close data at 18:00 Abidjan
    was labelled STALE every evening because the market is closed."""
    ov = _overview(market_open=False, last_snapshot_utc=_at(-timedelta(minutes=45)))
    assert not ov.is_stale


def test_snapshot_older_than_closed_threshold_is_stale():
    """Two missed outside-hours polls (hourly at :17) — either the
    scraper is stuck or the source is down. Either way, flag it."""
    ov = _overview(market_open=False, last_snapshot_utc=_at(-timedelta(hours=3)))
    assert ov.is_stale


def test_unparseable_timestamp_is_stale():
    """Fail loud on malformed input rather than silently hiding the
    badge."""
    ov = _overview(market_open=True, last_snapshot_utc="not-a-timestamp")
    assert ov.is_stale


def test_naive_timestamp_is_treated_as_utc():
    """`utc_iso()` in `brvm.clock` produces timezone-aware strings, but
    a legacy row could be stored naive. Treat naive as UTC so a fresh
    row still renders as fresh."""
    naive = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    ov = _overview(market_open=True, last_snapshot_utc=naive)
    assert not ov.is_stale
