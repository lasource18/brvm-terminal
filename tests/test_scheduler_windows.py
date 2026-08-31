from freezegun import freeze_time

from kodji.clock import is_market_open


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
    from kodji.jobs.scheduler import build_scheduler

    sched = build_scheduler()
    ids = {j.id for j in sched.get_jobs()}
    assert "snapshot_market_hours" in ids
    assert "snapshot_hourly_outside" in ids
    assert "news_market_hours" in ids
    assert "news_hourly_outside" in ids
    assert "news_tag_market_hours" in ids
    assert "news_tag_hourly_outside" in ids
    assert "sector_enrichment_weekly" in ids
    assert "fundamentals_extract_daily" in ids
    # Phase 6a jobs.
    assert "alerts_evaluate_market_hours" in ids
    assert "alerts_evaluate_hourly_outside" in ids
    assert "alerts_deliver_every_5min" in ids
    # Phase 6b jobs.
    assert "brief_daily" in ids
    # Phase 6c jobs.
    assert "analyst_notes_weekly" in ids
    # F-27: filings pull daily, ahead of OCR + extract.
    assert "filings_pull_daily" in ids
    # F-04: BOC reconciliation daily.
    assert "boc_reconcile_daily" in ids
    # F-18: brief runs Mon-Fri at 15:45 Abidjan (shifted from 15:30
    # so the last outside-hours tag pass at :31 has settled).
    brief = next(j for j in sched.get_jobs() if j.id == "brief_daily")
    cron = brief.trigger
    assert str(cron.fields[cron.FIELD_NAMES.index("hour")]) == "15"
    assert str(cron.fields[cron.FIELD_NAMES.index("minute")]) == "45"


@freeze_time("2026-01-01 15:45:00", tz_offset=0)  # New Year's Day
def test_brief_job_skips_on_holiday(monkeypatch):
    """F-18: even on Mon-Fri, `_brief_job` must skip when
    `is_market_holiday(today)` — a "session recap" of the prior day's
    close on a public holiday is stale data, not a brief."""
    from kodji.jobs import scheduler
    called: list[bool] = []
    monkeypatch.setattr(scheduler, "generate_brief",
                        lambda *a, **kw: called.append(True))
    scheduler._brief_job()
    assert called == []


@freeze_time("2026-08-19 15:45:00", tz_offset=0)  # regular weekday
def test_brief_job_runs_on_ordinary_weekday(monkeypatch):
    """Sanity check: `_brief_job` still calls generate_brief when not a
    holiday. Guards against a regression that gates the entire cron."""
    from kodji.jobs import scheduler

    class _Result:
        def as_dict(self):
            return {}

    called: list[bool] = []
    monkeypatch.setattr(
        scheduler, "generate_brief",
        lambda *a, **kw: (called.append(True), _Result())[1],
    )
    scheduler._brief_job()
    assert called == [True]
