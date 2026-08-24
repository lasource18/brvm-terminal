import pytest

from brvm.db import connect, ensure_migrations_table
from brvm.models import Security
from brvm.store import securities as sec_repo


@pytest.fixture
def search_env(monkeypatch, tmp_path):
    from pathlib import Path

    from brvm.config import reset_settings_cache

    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from brvm.services import search as search_mod

    root = Path(__file__).resolve().parents[1]
    with connect(db_path) as conn:
        ensure_migrations_table(conn)
        conn.executescript((root / "migrations" / "0001_init.sql").read_text())
        conn.executescript((root / "migrations" / "0002_watchlists.sql").read_text())
        conn.commit()
        sec_repo.upsert(
            conn,
            [
                Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
                Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
                Security(ticker="ONTBF", name="ONATEL BF", kind="equity", country="BF"),
                Security(ticker="BOAC", name="BANK OF AFRICA CI", kind="equity", country="CI"),
                Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
            ],
        )
    yield search_mod
    reset_settings_cache()


def test_exact_ticker_beats_prefix(search_env):
    hits = search_env.search("ORAC")
    assert hits[0].ticker == "ORAC"


def test_prefix_ticker_ranks_before_name(search_env):
    hits = search_env.search("ONT")
    # Both ONATEL (ticker prefix) and other names matching ONT could hit.
    assert hits[0].ticker == "ONTBF"


def test_case_insensitive_and_name_match(search_env):
    hits = search_env.search("sonatel")
    assert any(h.ticker == "SNTS" for h in hits)


def test_partial_name(search_env):
    hits = search_env.search("bank of")
    assert any(h.ticker == "BOAC" for h in hits)


def test_empty_query(search_env):
    assert search_env.search("") == []
    assert search_env.search("   ") == []


def test_limit_respected(search_env):
    hits = search_env.search("B", limit=2)
    assert len(hits) == 2
