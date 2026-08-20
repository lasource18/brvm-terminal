import pytest

from brvm.db import connect, ensure_migrations_table
from brvm.models import Quote, Security
from brvm.store import quotes as quotes_repo
from brvm.store import securities as sec_repo


@pytest.fixture
def dir_env(monkeypatch, tmp_path):
    import importlib
    from pathlib import Path

    import brvm.config as cfg

    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    importlib.reload(cfg)
    import brvm.services.directory as dir_mod

    importlib.reload(dir_mod)

    root = Path(__file__).resolve().parents[1]
    with connect(db_path) as conn:
        ensure_migrations_table(conn)
        conn.executescript((root / "migrations" / "0001_init.sql").read_text())
        conn.executescript((root / "migrations" / "0002_watchlists.sql").read_text())
        conn.commit()
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
    yield dir_mod
    importlib.reload(cfg)
    importlib.reload(dir_mod)


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
