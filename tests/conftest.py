"""Shared pytest fixtures."""

from __future__ import annotations

import importlib
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

# Modules that capture `settings` at import time; reloading them is how we
# point the app at a per-test tmp SQLite file.
_RELOADABLE = (
    "brvm.services.news",
    "brvm.services.market",
    "brvm.services.history",
    "brvm.services.watchlist",
    "brvm.services.quotes",
    "brvm.services.company",
    "brvm.services.search",
    "brvm.services.directory",
    "brvm.services.tagging",
    "brvm.apps.web.routes.pages",
    "brvm.apps.web.routes.fragments",
    "brvm.apps.web.routes.api",
    "brvm.apps.web.main",
)


def apply_migrations(conn) -> None:
    """Apply every migration in order. Tests use this instead of naming
    files so a new migration doesn't need a sweep through the suite."""
    from brvm.db import ensure_migrations_table

    ensure_migrations_table(conn)
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.executescript(f.read_text(encoding="utf-8"))
    conn.commit()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "brvm.sqlite"


def _reload_all() -> None:
    import brvm.config as cfg

    importlib.reload(cfg)
    for name in _RELOADABLE:
        importlib.reload(importlib.import_module(name))


def _seed(db_path: Path) -> None:
    from brvm.db import connect
    from brvm.models import IndexLevel, Quote, Security
    from brvm.store import quotes as quotes_repo
    from brvm.store import securities as sec_repo

    with connect(db_path) as conn:
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
            ],
        )


@pytest.fixture
def client(monkeypatch, tmp_path):
    """TestClient over a fresh, seeded SQLite DB with the APScheduler mocked."""
    from fastapi.testclient import TestClient

    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    _reload_all()
    _seed(db_path)

    from brvm.apps.web.main import app

    with patch("brvm.apps.web.main.build_scheduler") as bs:
        bs.return_value.get_jobs.return_value = []
        with TestClient(app) as c:
            yield c

    _reload_all()
