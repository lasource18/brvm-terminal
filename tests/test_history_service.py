from datetime import date
from pathlib import Path

import pytest

from brvm.db import connect
from brvm.models import DailyBar, IndexLevel, Quote, Security
from brvm.store import quotes as quotes_repo
from brvm.store import securities as sec_repo


def _init(db_path: Path) -> None:
    from .conftest import apply_migrations

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(
            conn,
            [
                Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
                Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
            ],
        )


@pytest.fixture
def history_env(monkeypatch, tmp_path):
    import importlib

    import brvm.config as cfg

    db = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db))
    importlib.reload(cfg)
    import brvm.services.history as history_mod

    importlib.reload(history_mod)
    _init(db)
    history_mod.clear_cache()
    yield history_mod
    importlib.reload(cfg)
    importlib.reload(history_mod)


def _fake_bars(n: int = 5) -> list[DailyBar]:
    base = date(2026, 8, 18)
    from datetime import timedelta

    return [
        DailyBar(
            ticker="SNTS",
            session_date=base - timedelta(days=i),
            open=32000 + i,
            high=32500 + i,
            low=31900 + i,
            close=32000 + i,
            volume=1000 * (i + 1),
            turnover=32_000_000 * (i + 1),
            source="sikafinance",
        )
        for i in range(n)
    ]


def test_cache_populates_on_miss_and_hits_on_second_call(history_env, monkeypatch):
    calls = {"n": 0}

    def fake_fetch(ticker, country, client=None):
        calls["n"] += 1
        return _fake_bars(5)

    monkeypatch.setattr("brvm.services.history.sikafinance.fetch_historique", fake_fetch)

    bars1 = history_env.get_history("SNTS", "SN")
    assert len(bars1) == 5
    assert calls["n"] == 1

    bars2 = history_env.get_history("SNTS", "SN")
    assert len(bars2) == 5
    assert calls["n"] == 1  # served from cache


def test_falls_back_to_db_when_fetch_fails_and_db_has_rows(history_env, monkeypatch):
    import httpx

    with connect(history_env._db_path()) as conn:
        quotes_repo.upsert_daily_bars(conn, _fake_bars(3))

    # Force a stale cache so the code path re-consults the DB / network.
    history_env.clear_cache()

    def boom(*a, **kw):
        raise httpx.HTTPError("network down")

    monkeypatch.setattr("brvm.services.history.sikafinance.fetch_historique", boom)
    # Also make _newest_ingested_age return infinity to bypass the DB-fresh branch.
    monkeypatch.setattr(history_env, "_newest_ingested_age", lambda t: 10**9)

    bars = history_env.get_history("SNTS", "SN")
    assert len(bars) == 3


def test_intraday_overlay_prepends_todays_bar_from_snapshots(history_env, monkeypatch):
    """Today's session is missing from `daily_bars` during market hours
    (sikafinance historique publishes T+1). The overlay stitches today's
    candle in from `quote_snapshots` so the chart doesn't stop at
    yesterday's close."""
    from datetime import date as _date

    # Freeze "today" so the test is deterministic across days.
    today = _date(2026, 8, 24)
    monkeypatch.setattr(history_env, "session_date_for", lambda dt=None: today)

    # Seed yesterday and prior in daily_bars.
    with connect(history_env._db_path()) as conn:
        quotes_repo.upsert_daily_bars(conn, [
            DailyBar(ticker="SNTS", session_date=_date(2026, 8, 21),
                     open=36000, high=37500, low=35900, close=37200, volume=1000,
                     source="sikafinance"),
            DailyBar(ticker="SNTS", session_date=_date(2026, 8, 20),
                     open=35800, high=36200, low=35700, close=36000, volume=800,
                     source="sikafinance"),
        ])
        # Seed a spread of today's snapshots so open/high/low/close aggregate.
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="SNTS", source="sikafinance",
                  last=36980, open=36980, high=36980, low=36980,
                  volume=4546),
            Quote(ticker="SNTS", source="sikafinance",
                  last=37000, open=36980, high=37000, low=36980,
                  volume=4931),
            Quote(ticker="SNTS", source="sikafinance",
                  last=36900, open=36980, high=37000, low=36900,
                  volume=5045),
            Quote(ticker="SNTS", source="sikafinance",
                  last=37000, open=36980, high=37000, low=36900,
                  volume=5366),
        ])
        # Stamp all four snapshots at 12:00-15:00 UTC so the market-hours
        # filter (>= 09:00 UTC on today) picks them all up. `insert_snapshots`
        # writes captured_utc=now which wouldn't match a frozen `today`.
        conn.execute(
            "UPDATE quote_snapshots SET captured_utc = "
            "'2026-08-24T' || printf('%02d', rowid + 11) || ':00:00Z'"
        )
        conn.commit()

    # Neuter the network + freshness path so the DB read wins.
    monkeypatch.setattr("brvm.services.history.sikafinance.fetch_historique",
                        lambda *a, **kw: [])
    monkeypatch.setattr(history_env, "_newest_ingested_age", lambda t: 0)

    bars = history_env.get_history("SNTS", "SN")
    # Newest-first: today's synthetic bar, then yesterday, then prior.
    assert bars[0].session_date == today
    assert bars[0].source == "intraday_snapshot"
    assert bars[0].open == 36980
    assert bars[0].close == 37000
    assert bars[0].high == 37000        # max across snapshots
    assert bars[0].low == 36900         # min across snapshots
    assert bars[0].volume == 5366       # latest cumulative volume
    # Historical bars unchanged.
    assert bars[1].session_date == _date(2026, 8, 21)


def test_intraday_overlay_skips_when_today_already_in_daily_bars(history_env, monkeypatch):
    """Once the sikafinance nightly publishes today's row into
    `daily_bars`, the intraday overlay should stand down so we don't
    double-count."""
    from datetime import date as _date

    today = _date(2026, 8, 24)
    monkeypatch.setattr(history_env, "session_date_for", lambda dt=None: today)

    with connect(history_env._db_path()) as conn:
        quotes_repo.upsert_daily_bars(conn, [
            DailyBar(ticker="SNTS", session_date=today,
                     open=36000, high=37200, low=35800, close=37100, volume=8000,
                     source="sikafinance"),
        ])
        # Snapshots also present but the historique already covers today.
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="SNTS", source="sikafinance",
                  last=37200, open=36000, high=37200, low=35800, volume=8500),
        ])
        conn.execute("UPDATE quote_snapshots SET captured_utc = '2026-08-24T13:00:00Z'")
        conn.commit()

    monkeypatch.setattr("brvm.services.history.sikafinance.fetch_historique",
                        lambda *a, **kw: [])
    monkeypatch.setattr(history_env, "_newest_ingested_age", lambda t: 0)

    bars = history_env.get_history("SNTS", "SN")
    # Only the historique row for today — no synthetic prepend.
    assert len(bars) == 1
    assert bars[0].source == "sikafinance"
    assert bars[0].close == 37100


def test_intraday_overlay_ignores_premarket_snapshots(history_env, monkeypatch):
    """Sikafinance's cotation page still serves yesterday's cumulative
    data until the market opens; the overnight poller (00:17 / 01:17 /
    03:17 UTC) therefore captures yesterday's numbers under today's
    `captured_utc`. Those must not leak into today's synthetic candle."""
    from datetime import date as _date

    today = _date(2026, 8, 24)
    monkeypatch.setattr(history_env, "session_date_for", lambda dt=None: today)

    with connect(history_env._db_path()) as conn:
        quotes_repo.upsert_daily_bars(conn, [
            DailyBar(ticker="SNTS", session_date=_date(2026, 8, 21),
                     open=36000, high=37500, low=35900, close=37200, volume=1000,
                     source="sikafinance"),
        ])
        # Two overnight snapshots showing yesterday's cumulative data,
        # then two market-hours snapshots showing today's.
        quotes_repo.insert_snapshots(conn, [
            # Pre-market — stale data that must be excluded
            Quote(ticker="SNTS", source="sikafinance",
                  last=37200, open=34400, high=37200, low=34400, volume=13615),
            Quote(ticker="SNTS", source="sikafinance",
                  last=37200, open=34400, high=37200, low=34400, volume=13615),
            # Market hours — real today data
            Quote(ticker="SNTS", source="sikafinance",
                  last=36980, open=36980, high=36980, low=36980, volume=4546),
            Quote(ticker="SNTS", source="sikafinance",
                  last=37000, open=36980, high=37000, low=36900, volume=5366),
        ])
        # Rows 1-2 stamped at 00:17 / 03:17 UTC (pre-market),
        # rows 3-4 stamped at 12:00 / 13:00 UTC (market hours).
        conn.execute(
            "UPDATE quote_snapshots SET captured_utc = "
            "CASE rowid "
            "WHEN 1 THEN '2026-08-24T00:17:00Z' "
            "WHEN 2 THEN '2026-08-24T03:17:00Z' "
            "WHEN 3 THEN '2026-08-24T12:00:00Z' "
            "WHEN 4 THEN '2026-08-24T13:00:00Z' END"
        )
        conn.commit()

    monkeypatch.setattr("brvm.services.history.sikafinance.fetch_historique",
                        lambda *a, **kw: [])
    monkeypatch.setattr(history_env, "_newest_ingested_age", lambda t: 0)

    bars = history_env.get_history("SNTS", "SN")
    intraday = bars[0]
    assert intraday.session_date == today
    assert intraday.source == "intraday_snapshot"
    # Only market-hours captures contribute: open=36980, low=36900,
    # high=37000, close=37000 (latest last), vol=5366 (latest cumulative).
    # If pre-market rows had leaked, open would be 34400 and low would
    # be 34400 too.
    assert intraday.open == 36980
    assert intraday.low == 36900
    assert intraday.high == 37000
    assert intraday.close == 37000
    assert intraday.volume == 5366


def test_intraday_overlay_pre_market_only_returns_history_unchanged(history_env, monkeypatch):
    """Pre-market before the first real trade — only overnight stale
    captures exist. The overlay should skip so we don't fabricate a
    candle from yesterday's data."""
    from datetime import date as _date

    today = _date(2026, 8, 24)
    monkeypatch.setattr(history_env, "session_date_for", lambda dt=None: today)

    with connect(history_env._db_path()) as conn:
        quotes_repo.upsert_daily_bars(conn, [
            DailyBar(ticker="SNTS", session_date=_date(2026, 8, 21),
                     open=36000, high=37500, low=35900, close=37200, volume=1000,
                     source="sikafinance"),
        ])
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="SNTS", source="sikafinance",
                  last=37200, open=34400, high=37200, low=34400, volume=13615),
        ])
        conn.execute("UPDATE quote_snapshots SET captured_utc = '2026-08-24T03:17:00Z'")
        conn.commit()

    monkeypatch.setattr("brvm.services.history.sikafinance.fetch_historique",
                        lambda *a, **kw: [])
    monkeypatch.setattr(history_env, "_newest_ingested_age", lambda t: 0)

    bars = history_env.get_history("SNTS", "SN")
    # No overlay — chart stops cleanly at Friday's close.
    assert len(bars) == 1
    assert bars[0].session_date == _date(2026, 8, 21)


def test_intraday_overlay_no_snapshots_no_overlay(history_env, monkeypatch):
    """Weekend / scheduler down / pre-market — no snapshots for today,
    so the returned series stops at yesterday's close cleanly."""
    from datetime import date as _date

    today = _date(2026, 8, 24)
    monkeypatch.setattr(history_env, "session_date_for", lambda dt=None: today)

    with connect(history_env._db_path()) as conn:
        quotes_repo.upsert_daily_bars(conn, [
            DailyBar(ticker="SNTS", session_date=_date(2026, 8, 21),
                     open=36000, high=37200, low=35800, close=37100, volume=8000,
                     source="sikafinance"),
        ])

    monkeypatch.setattr("brvm.services.history.sikafinance.fetch_historique",
                        lambda *a, **kw: [])
    monkeypatch.setattr(history_env, "_newest_ingested_age", lambda t: 0)

    bars = history_env.get_history("SNTS", "SN")
    assert len(bars) == 1
    assert bars[0].session_date == _date(2026, 8, 21)


def test_backfill_all_walks_every_active_equity(monkeypatch, history_env):
    """The weekly backfill must hit every ticker sikafinance has, not
    just the ones a user has clicked into."""
    from datetime import date as _date
    from datetime import timedelta

    with connect(history_env._db_path()) as conn:
        sec_repo.upsert(conn, [
            Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
            Security(ticker="ONTBF", name="ONATEL BF", kind="equity", country="BF"),
            # Non-equity — must be skipped.
            Security(ticker="BRVMPR", name="BRVM PRESTIGE", kind="index"),
        ])

    calls: list[tuple[str, str | None]] = []

    def fake_fetch(ticker, country, client=None):
        calls.append((ticker, country))
        base = _date(2026, 8, 21)
        return [
            DailyBar(ticker=ticker, session_date=base - timedelta(days=i),
                     close=100.0 + i, source="sikafinance")
            for i in range(5)
        ]

    monkeypatch.setattr(
        "brvm.services.history.sikafinance.fetch_historique", fake_fetch
    )

    class _NoOpClient:
        def close(self):
            pass

    counts = history_env.backfill_all(client=_NoOpClient(), delay_between_requests_s=0)

    # SNTS (from fixture) + ORAC + ONTBF; BRVMPR excluded.
    assert counts["considered"] == 3
    assert counts["fetched"] == 3
    assert counts["bars_inserted"] == 15
    assert sorted(t for t, _ in calls) == ["ONTBF", "ORAC", "SNTS"]

    with connect(history_env._db_path()) as conn:
        n = conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM daily_bars"
        ).fetchone()[0]
    assert n == 3


def test_backfill_all_skips_recently_ingested(monkeypatch, history_env):
    """Rerunning within min_age_days is a full no-op."""
    with connect(history_env._db_path()) as conn:
        quotes_repo.upsert_daily_bars(conn, _fake_bars(3))

    calls = {"n": 0}

    def fake_fetch(ticker, country, client=None):
        calls["n"] += 1
        return _fake_bars(3)

    monkeypatch.setattr(
        "brvm.services.history.sikafinance.fetch_historique", fake_fetch
    )

    class _NoOpClient:
        def close(self):
            pass

    counts = history_env.backfill_all(
        client=_NoOpClient(), min_age_days=7, delay_between_requests_s=0
    )
    assert counts["up_to_date"] == 1
    assert calls["n"] == 0


def test_backfill_all_survives_http_errors(monkeypatch, history_env):
    """One flaky ticker mustn't abort the whole pass."""
    import httpx as _httpx

    with connect(history_env._db_path()) as conn:
        sec_repo.upsert(conn, [
            Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
        ])

    def flaky(ticker, country, client=None):
        if ticker == "ORAC":
            raise _httpx.HTTPError("simulated timeout")
        return _fake_bars(2)

    monkeypatch.setattr(
        "brvm.services.history.sikafinance.fetch_historique", flaky
    )

    class _NoOpClient:
        def close(self):
            pass

    counts = history_env.backfill_all(client=_NoOpClient(), delay_between_requests_s=0)
    assert counts["failed"] == 1
    assert counts["fetched"] == 1  # SNTS came back fine


def test_indices_load_from_index_levels_and_skip_network(history_env, monkeypatch):
    from datetime import timedelta

    def boom(*a, **kw):  # would fire if the code path attempted the fetch
        raise AssertionError("fetch_historique should not be called for indices")

    monkeypatch.setattr("brvm.services.history.sikafinance.fetch_historique", boom)

    base = date(2026, 8, 18)
    levels = [
        IndexLevel(
            ticker="BRVMC",
            session_date=base - timedelta(days=i),
            level=300.0 + i,
            change_pct=0.1 * i,
            source="sikafinance",
        )
        for i in range(4)
    ]
    with connect(history_env._db_path()) as conn:
        quotes_repo.upsert_index_levels(conn, levels)

    history_env.clear_cache()
    bars = history_env.get_history("BRVMC")
    # newest-first: 2026-08-18 (i=0, level=300) → 2026-08-15 (i=3, level=303)
    assert [b.close for b in bars] == [300.0, 301.0, 302.0, 303.0]
    assert all(b.open is None and b.high is None and b.volume is None for b in bars)
