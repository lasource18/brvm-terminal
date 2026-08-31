from datetime import date
from pathlib import Path

import pytest

from kodji.db import connect
from kodji.models import IndexLevel, Quote, Security
from kodji.store import quotes as quotes_repo
from kodji.store import securities as sec_repo

from .conftest import apply_migrations


def _seed(db_path: Path) -> None:
    with connect(db_path) as conn:
        # Full migration set — Phase 3c's `market.overview()` reaches into
        # corporate_actions (introduced in 0003), so an 0001+0002-only seed
        # would break the overview test. Historically this test relied on
        # leaky module-singleton state to paper over the mismatch.
        apply_migrations(conn)
        sec_repo.upsert(
            conn,
            [
                Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
                Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
                Security(ticker="SPHC", name="SAPH CI", kind="equity", country="CI"),
                Security(ticker="CFAC", name="CFAO CI", kind="equity", country="CI"),
                Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
                Security(ticker="BRVM30", name="BRVM 30", kind="index"),
                Security(ticker="BRVMPR", name="BRVM - PRESTIGE", kind="index"),
            ],
        )
        quotes_repo.insert_snapshots(
            conn,
            [
                Quote(ticker="SNTS", source="sikafinance", last=32500, change_pct=1.88,
                      volume=3006, turnover=97_695_000),
                Quote(ticker="ORAC", source="sikafinance", last=19000, change_pct=-0.5,
                      volume=1000, turnover=19_000_000),
                Quote(ticker="SPHC", source="sikafinance", last=8990, change_pct=7.02,
                      volume=52971, turnover=476_209_290),
                Quote(ticker="CFAC", source="sikafinance", last=1600, change_pct=-3.90,
                      volume=4878, turnover=7_804_800),
            ],
        )
        quotes_repo.upsert_index_levels(
            conn,
            [
                IndexLevel(ticker="BRVMC", session_date=date(2026, 8, 19),
                           level=507.13, change_pct=1.16, source="sikafinance"),
                IndexLevel(ticker="BRVM30", session_date=date(2026, 8, 19),
                           level=242.89, change_pct=1.60, source="sikafinance"),
            ],
        )


@pytest.fixture
def db(monkeypatch, tmp_path):
    """Point services at a fresh tmp DB via env + settings-cache reset.
    The lazy proxy in `kodji.config` re-reads env on the next attribute
    access, so no module reload is needed."""
    from kodji.config import reset_settings_cache

    db_path = tmp_path / "kodji.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    _seed(db_path)
    yield db_path
    reset_settings_cache()


def test_top_by_turnover(db):
    from kodji.services import market

    rows = market.top_by_turnover(3)
    assert [r.ticker for r in rows] == ["SPHC", "SNTS", "ORAC"]


def test_gainers_orders_by_change_desc_and_filters_positive(db):
    from kodji.services import market

    rows = market.gainers(5)
    assert [r.ticker for r in rows] == ["SPHC", "SNTS"]
    assert all(r.change_pct and r.change_pct > 0 for r in rows)


def test_losers_orders_by_change_asc_and_filters_negative(db):
    from kodji.services import market

    rows = market.losers(5)
    assert [r.ticker for r in rows] == ["CFAC", "ORAC"]
    assert all(r.change_pct and r.change_pct < 0 for r in rows)


def test_indices_tiles(db):
    from kodji.services import market

    tiles = market.indices_tiles()
    assert [t.ticker for t in tiles] == ["BRVMC", "BRVM30", "BRVMPR"]
    brvmc = tiles[0]
    assert brvmc.level == 507.13
    assert brvmc.session_date == date(2026, 8, 19)
    # BRVMPR was seeded as a security but has no level yet.
    assert tiles[2].level is None


def test_overview_bundles_everything(db):
    from kodji.services import market

    ov = market.overview(limit=3)
    assert len(ov.indices) == 3
    assert len(ov.gainers) == 2
    assert len(ov.losers) == 2
    assert len(ov.turnover_leaders) == 3
    assert ov.generated_utc.endswith("Z")


def test_get_security(db):
    from kodji.services import market

    sv = market.get_security("SNTS")
    assert sv is not None
    assert sv.name == "SONATEL"
    assert sv.quote is not None
    assert sv.quote.last == 32500
