from datetime import date
from pathlib import Path

import pytest

from brvm.db import connect, ensure_migrations_table
from brvm.models import DailyBar, Security
from brvm.store import securities as sec_repo


def _init(db_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    with connect(db_path) as conn:
        ensure_migrations_table(conn)
        conn.executescript((root / "migrations" / "0001_init.sql").read_text())
        conn.executescript((root / "migrations" / "0002_watchlists.sql").read_text())
        conn.commit()
        sec_repo.upsert(conn, [Security(ticker="SNTS", name="SONATEL",
                                        kind="equity", country="SN")])


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

    from brvm.store import quotes as quotes_repo

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
