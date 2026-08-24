"""Tests for the sector-enrichment service."""

from __future__ import annotations

from pathlib import Path

import pytest

from brvm.config import reset_settings_cache
from brvm.db import connect, ensure_migrations_table
from brvm.models import Security
from brvm.store import securities as sec_repo


@pytest.fixture
def enrich_env(monkeypatch, tmp_path):
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from brvm.services import enrichment as enrich_mod

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
                Security(
                    ticker="BOAC",
                    name="BANK OF AFRICA CI",
                    kind="equity",
                    country="CI",
                    sector="Banques",
                ),
                Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
            ],
        )
    yield db_path, enrich_mod
    reset_settings_cache()


def test_list_missing_sector_only_equities_without_sector(enrich_env):
    db_path, _ = enrich_env
    with connect(db_path) as conn:
        rows = sec_repo.list_missing_sector(conn)
    tickers = [r["ticker"] for r in rows]
    assert tickers == ["ORAC", "SNTS"]


def test_enrich_sectors_updates_from_sikafinance(enrich_env, monkeypatch):
    db_path, enrich_mod = enrich_env

    def fake_secteur(ticker, country, client=None):
        return {
            "SNTS": {"sector": "BRVM - TELECOMMUNICATIONS", "peers": []},
            "ORAC": {"sector": "BRVM - TELECOMMUNICATIONS", "peers": []},
        }[ticker]

    monkeypatch.setattr(
        "brvm.services.enrichment.sikafinance.fetch_secteur", fake_secteur
    )
    counts = enrich_mod.enrich_sectors(sleep_s=0)
    assert counts == {"candidates": 2, "updated": 2, "still_missing": 0}
    with connect(db_path) as conn:
        rows = {
            r["ticker"]: r["sector"]
            for r in conn.execute("SELECT ticker, sector FROM securities").fetchall()
        }
    assert rows["SNTS"] == "BRVM - TELECOMMUNICATIONS"
    assert rows["ORAC"] == "BRVM - TELECOMMUNICATIONS"
    assert rows["BOAC"] == "Banques"  # untouched


def test_enrich_sectors_falls_back_to_afx(enrich_env, monkeypatch):
    db_path, enrich_mod = enrich_env

    def fake_secteur(ticker, country, client=None):
        return {"sector": None, "peers": []}

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    def fake_get(url):
        # ORAC gets a sector via afx; SNTS returns an empty factsheet.
        if "orac" in url:
            return FakeResponse(
                '<div data-fact><dl>'
                '<div><dt>Sector</dt><dd>Telecommunications</dd></div>'
                '</dl></div>'
            )
        return FakeResponse("<html></html>")

    monkeypatch.setattr(
        "brvm.services.enrichment.sikafinance.fetch_secteur", fake_secteur
    )

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, url):
            return fake_get(url)

    monkeypatch.setattr("brvm.services.enrichment.make_client", lambda: FakeClient())

    counts = enrich_mod.enrich_sectors(sleep_s=0)
    assert counts["candidates"] == 2
    assert counts["updated"] == 1
    assert counts["still_missing"] == 1
    with connect(db_path) as conn:
        rows = {
            r["ticker"]: r["sector"]
            for r in conn.execute("SELECT ticker, sector FROM securities").fetchall()
        }
    assert rows["ORAC"] == "Telecommunications"
    assert rows["SNTS"] is None


def test_enrich_sectors_noop_when_all_populated(enrich_env, monkeypatch):
    db_path, enrich_mod = enrich_env
    with connect(db_path) as conn:
        sec_repo.update_sector(conn, "SNTS", "Telecoms")
        sec_repo.update_sector(conn, "ORAC", "Telecoms")
    counts = enrich_mod.enrich_sectors(sleep_s=0)
    assert counts == {"candidates": 0, "updated": 0, "still_missing": 0}
