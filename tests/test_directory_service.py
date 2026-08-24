from datetime import date

import pytest

from brvm.db import connect
from brvm.models import DailyBar, IndexLevel, Quote, Security
from brvm.store import quotes as quotes_repo
from brvm.store import securities as sec_repo

from .conftest import apply_migrations


@pytest.fixture
def dir_env(monkeypatch, tmp_path):
    import importlib

    import brvm.config as cfg

    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    importlib.reload(cfg)
    import brvm.services.directory as dir_mod

    importlib.reload(dir_mod)

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(
            conn,
            [
                Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN",
                         sector="Telecoms"),
                Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI",
                         sector="Telecoms"),
                Security(ticker="BOAC", name="BANK OF AFRICA CI", kind="equity",
                         country="CI", sector="Banques"),
                Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
            ],
        )
        quotes_repo.insert_snapshots(
            conn,
            [
                Quote(ticker="SNTS", source="sikafinance", last=32500, change_pct=1.88),
                Quote(ticker="ORAC", source="sikafinance", last=19000, change_pct=-0.5),
            ],
        )
        quotes_repo.upsert_index_levels(
            conn,
            [
                IndexLevel(
                    ticker="BRVMC",
                    session_date=date(2026, 8, 20),
                    level=307.42,
                    change_pct=0.53,
                    source="sikafinance",
                ),
            ],
        )
    yield dir_mod
    importlib.reload(cfg)
    importlib.reload(dir_mod)


def _seed_period_bars(db_path, monkeypatch, today: date, dir_mod):
    """Seed a small daily-bar history so period-return columns have
    reference points to compute against, and freeze `session_date_for`
    on the module so the test doesn't drift with the real clock."""
    monkeypatch.setattr(dir_mod, "session_date_for", lambda dt=None: today)

    # SNTS: 100 → 105 → 110 → 120 → 132 spanning today, -7d, -30d, -90d,
    # and prior year. Reference formula for each period:
    #   1W:  from -7d (110)  → (132-110)/110 = +20%
    #   1M:  from -30d (105) → (132-105)/105 ≈ +25.71%
    #   3M:  from -90d (100) → (132-100)/100 = +32%
    #   YTD: from prior year close (90) → (132-90)/90 ≈ +46.67%
    from datetime import timedelta as td
    bars = [
        DailyBar(ticker="SNTS", session_date=today - td(days=90), close=100.0, source="sikafinance"),
        DailyBar(ticker="SNTS", session_date=today - td(days=30), close=105.0, source="sikafinance"),
        DailyBar(ticker="SNTS", session_date=today - td(days=7),  close=110.0, source="sikafinance"),
        DailyBar(ticker="SNTS", session_date=today - td(days=1),  close=120.0, source="sikafinance"),
        DailyBar(ticker="SNTS", session_date=today,               close=132.0, source="sikafinance"),
        # Prior-year close, one day before Jan 1 of `today`'s year.
        DailyBar(ticker="SNTS",
                 session_date=date(today.year - 1, 12, 31), close=90.0,
                 source="sikafinance"),
    ]
    with connect(db_path) as conn:
        quotes_repo.upsert_daily_bars(conn, bars)


def test_list_all(dir_env):
    rows = dir_env.list_directory()
    tickers = [r.ticker for r in rows]
    assert set(tickers) == {"SNTS", "ORAC", "BOAC", "BRVMC"}


def test_filter_by_country(dir_env):
    rows = dir_env.list_directory(country="CI")
    assert set(r.ticker for r in rows) == {"ORAC", "BOAC"}


def test_filter_by_sector(dir_env):
    rows = dir_env.list_directory(sector="Telecoms")
    assert set(r.ticker for r in rows) == {"SNTS", "ORAC"}


def test_filter_by_kind_equity(dir_env):
    rows = dir_env.list_directory(kind="equity")
    assert all(r.kind == "equity" for r in rows)
    assert "BRVMC" not in [r.ticker for r in rows]


def test_text_filter(dir_env):
    rows = dir_env.list_directory(q="sonatel")
    assert [r.ticker for r in rows] == ["SNTS"]


def test_distinct_helpers(dir_env):
    assert set(dir_env.distinct_countries()) == {"SN", "CI"}
    assert set(dir_env.distinct_sectors()) == {"Telecoms", "Banques"}


def test_row_carries_latest_quote(dir_env):
    rows = dir_env.list_directory(q="SNTS")
    assert rows[0].last == 32500
    assert rows[0].change_pct == 1.88


def test_row_carries_latest_index_level(dir_env):
    rows = dir_env.list_directory(q="BRVMC")
    assert rows[0].last == pytest.approx(307.42)
    assert rows[0].change_pct == pytest.approx(0.53)


# --- Phase 4e: period returns + column sort --------------------------------


def test_period_return_columns_populate_from_history(monkeypatch, tmp_path, dir_env):
    """Given a seeded price series spanning the four reference windows,
    the four `change_*_pct` columns should render the expected pcts."""
    from pathlib import Path
    db_path = Path(dir_env.settings.db_path)
    today = date(2026, 8, 24)
    _seed_period_bars(db_path, monkeypatch, today, dir_env)

    rows = dir_env.list_directory(q="SNTS")
    assert len(rows) == 1
    r = rows[0]
    # `last` still comes from the intraday snapshot fixture (32,500),
    # not from the daily-bar series — that's the intraday-vs-eod split
    # the docstring calls out.
    assert r.last == 32500
    # Period returns use daily_bars close 132.0 (today) against each ref.
    assert r.change_1w_pct == pytest.approx((132 - 110) / 110 * 100)
    assert r.change_1m_pct == pytest.approx((132 - 105) / 105 * 100)
    assert r.change_3m_pct == pytest.approx((132 - 100) / 100 * 100)
    assert r.change_ytd_pct == pytest.approx((132 - 90) / 90 * 100)


def test_period_returns_are_none_when_history_missing(dir_env):
    """A ticker with no daily_bars / index_levels at the reference dates
    should render None on each column, not zero. Verified against ORAC
    which has a snapshot but no historical bars in this fixture."""
    rows = dir_env.list_directory(q="ORAC")
    r = rows[0]
    assert r.change_1w_pct is None
    assert r.change_1m_pct is None
    assert r.change_3m_pct is None
    assert r.change_ytd_pct is None


def test_period_returns_use_closest_prior_bar(monkeypatch, tmp_path, dir_env):
    """The 1-week reference isn't exactly at T-7d for BRVM (weekends,
    holidays). The SQL joins on `MAX(session_date) WHERE session_date
    <= target` so a slightly-older bar takes over cleanly."""
    from datetime import timedelta as td
    from pathlib import Path
    db_path = Path(dir_env.settings.db_path)
    today = date(2026, 8, 24)  # Monday
    monkeypatch.setattr(dir_env, "session_date_for", lambda dt=None: today)

    # Only a bar 10 days back (a Friday) — the 1W query targets today - 7
    # (Monday); it should fall back to the -10d Friday.
    with connect(db_path) as conn:
        quotes_repo.upsert_daily_bars(conn, [
            DailyBar(ticker="SNTS", session_date=today - td(days=10), close=100.0,
                     source="sikafinance"),
            DailyBar(ticker="SNTS", session_date=today, close=110.0,
                     source="sikafinance"),
        ])

    r = dir_env.list_directory(q="SNTS")[0]
    assert r.change_1w_pct == pytest.approx((110 - 100) / 100 * 100)


def test_sort_by_column_desc_puts_biggest_movers_on_top(monkeypatch, tmp_path, dir_env):
    from pathlib import Path
    db_path = Path(dir_env.settings.db_path)
    today = date(2026, 8, 24)
    monkeypatch.setattr(dir_env, "session_date_for", lambda dt=None: today)

    from datetime import timedelta as td
    with connect(db_path) as conn:
        quotes_repo.upsert_daily_bars(conn, [
            # SNTS +20% over 7 days
            DailyBar(ticker="SNTS", session_date=today - td(days=7),  close=100.0, source="sikafinance"),
            DailyBar(ticker="SNTS", session_date=today,               close=120.0, source="sikafinance"),
            # ORAC -5% over 7 days
            DailyBar(ticker="ORAC", session_date=today - td(days=7),  close=100.0, source="sikafinance"),
            DailyBar(ticker="ORAC", session_date=today,               close=95.0,  source="sikafinance"),
            # BOAC flat
            DailyBar(ticker="BOAC", session_date=today - td(days=7),  close=200.0, source="sikafinance"),
            DailyBar(ticker="BOAC", session_date=today,               close=200.0, source="sikafinance"),
        ])

    rows = dir_env.list_directory(kind="equity", sort="change_1w_pct", direction="desc")
    equity_order = [r.ticker for r in rows]
    # SNTS first (biggest gain), BOAC (flat, 0), ORAC last (loss).
    assert equity_order == ["SNTS", "BOAC", "ORAC"]

    # Same key with direction=asc flips the order.
    rows_asc = dir_env.list_directory(kind="equity", sort="change_1w_pct", direction="asc")
    assert [r.ticker for r in rows_asc] == ["ORAC", "BOAC", "SNTS"]


def test_sort_with_nulls_last_regardless_of_direction(monkeypatch, tmp_path, dir_env):
    """Tickers with no reference price render NULL — those should always
    sink to the bottom so the visible ordering stays useful whether you
    click desc or asc."""
    from datetime import timedelta as td
    from pathlib import Path
    db_path = Path(dir_env.settings.db_path)
    today = date(2026, 8, 24)
    monkeypatch.setattr(dir_env, "session_date_for", lambda dt=None: today)

    with connect(db_path) as conn:
        # Only SNTS has a series; ORAC / BOAC will land as NULL for 1W%.
        quotes_repo.upsert_daily_bars(conn, [
            DailyBar(ticker="SNTS", session_date=today - td(days=7), close=100.0, source="sikafinance"),
            DailyBar(ticker="SNTS", session_date=today,              close=110.0, source="sikafinance"),
        ])

    for direction in ("desc", "asc"):
        rows = dir_env.list_directory(kind="equity", sort="change_1w_pct", direction=direction)
        tickers = [r.ticker for r in rows]
        # SNTS (non-null) sits at index 0; the two NULLs land after,
        # deterministically ordered by ticker tiebreak.
        assert tickers[0] == "SNTS"
        assert set(tickers[1:]) == {"BOAC", "ORAC"}


def test_unknown_sort_key_falls_back_to_default(dir_env):
    """Stale bookmark or hand-crafted URL with a garbage sort key
    shouldn't crash — the resolver drops back to the default order."""
    rows_default = dir_env.list_directory()
    rows_garbage = dir_env.list_directory(sort="DROP TABLE securities", direction="asc")
    assert [r.ticker for r in rows_garbage] == [r.ticker for r in rows_default]


def test_sort_by_text_column_is_ascending_by_default(dir_env):
    rows = dir_env.list_directory(kind="equity", sort="ticker")
    # Default direction for text columns is 'asc'.
    assert [r.ticker for r in rows] == ["BOAC", "ORAC", "SNTS"]
