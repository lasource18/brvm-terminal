"""Tests for services/news.poll_all: end-to-end ingest against fixtures."""

from __future__ import annotations

from pathlib import Path

from brvm.config import reset_settings_cache
from brvm.db import connect
from brvm.models import Security
from brvm.sources import sikafinance
from brvm.store import news as news_repo
from brvm.store import securities as sec_repo

from .conftest import apply_migrations


def _seed_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(
            conn,
            [
                # Names chosen to match the fixture data so ticker_hint fires.
                Security(ticker="SGBC", name="SGBCI", kind="equity", country="CI"),
                Security(ticker="TTLC", name="TOTAL CI", kind="equity", country="CI"),
                Security(ticker="SPHC", name="SAPH CI", kind="equity", country="CI"),
                Security(ticker="ETIT", name="ETI TG", kind="equity", country="TG"),
            ],
        )


def _patch_fetchers(monkeypatch, fixtures_dir: Path) -> None:
    news_html = (fixtures_dir / "sikafinance" / "actualites_brvm.html").read_text(encoding="utf-8")
    comm_html = (fixtures_dir / "sikafinance" / "communiques_brvm.html").read_text(encoding="utf-8")
    div_html = (fixtures_dir / "sikafinance" / "dividendes.html").read_text(encoding="utf-8")
    monkeypatch.setattr(
        sikafinance, "fetch_news_feed", lambda client=None: sikafinance.parse_news_feed(news_html)
    )
    monkeypatch.setattr(
        sikafinance,
        "fetch_communiques",
        lambda client=None: sikafinance.parse_communiques(comm_html),
    )
    monkeypatch.setattr(
        sikafinance,
        "fetch_dividendes",
        lambda client=None: sikafinance.parse_dividendes(div_html),
    )


def test_poll_all_ingests_and_dedupes(monkeypatch, tmp_path, fixtures_dir):
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from brvm.services import news as svc

    _seed_db(db_path)
    _patch_fetchers(monkeypatch, fixtures_dir)

    first = svc.poll_all()
    assert first["news_in"] > 0
    assert first["communiques_in"] > 0
    assert first["dividends_in"] > 0

    # Second poll on identical fixture data -> everything is a dupe /
    # already-known corporate action (updated, not inserted).
    second = svc.poll_all()
    assert second["news_in"] == 0
    assert second["news_dupe"] == first["news_in"]
    assert second["communiques_in"] == 0
    assert second["dividends_in"] == 0
    assert second["dividends_updated"] == first["dividends_in"]

    with connect(db_path) as conn:
        # ticker_hint should have been resolved for at least the common issuers.
        hints = {
            r["issuer_name"]: r["ticker_hint"]
            for r in conn.execute(
                "SELECT issuer_name, ticker_hint FROM news_items WHERE kind='communique'"
            ).fetchall()
        }
        assert hints.get("TOTAL CI") == "TTLC"
        assert hints.get("SAPH CI") == "SPHC"
        assert hints.get("SGBCI") == "SGBC"

        # Dividends -> corporate_actions carries at least SGBC/TTLC/SPHC.
        rows = news_repo.list_corporate_actions_upcoming(conn, days=60)
        tickers = {r["ticker"] for r in rows}
        assert {"SGBC", "TTLC", "SPHC"}.issubset(tickers)


def test_poll_all_survives_ticker_miss(monkeypatch, tmp_path, fixtures_dir):
    """If a communiqué issuer doesn't match any known security, the ingest
    still succeeds — ticker_hint is just NULL for that row."""
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from brvm.services import news as svc

    # Seed no securities at all -> nothing to match against.
    with connect(db_path) as conn:
        apply_migrations(conn)

    # Only feed news + communiqués -- dividends need securities (FK).
    news_html = (fixtures_dir / "sikafinance" / "actualites_brvm.html").read_text(encoding="utf-8")
    comm_html = (fixtures_dir / "sikafinance" / "communiques_brvm.html").read_text(encoding="utf-8")
    monkeypatch.setattr(
        sikafinance, "fetch_news_feed", lambda client=None: sikafinance.parse_news_feed(news_html)
    )
    monkeypatch.setattr(
        sikafinance,
        "fetch_communiques",
        lambda client=None: sikafinance.parse_communiques(comm_html),
    )
    monkeypatch.setattr(sikafinance, "fetch_dividendes", lambda client=None: [])

    counts = svc.poll_all()
    assert counts["news_in"] > 0
    assert counts["communiques_in"] > 0
    with connect(db_path) as conn:
        n_hinted = conn.execute(
            "SELECT COUNT(*) FROM news_items WHERE ticker_hint IS NOT NULL"
        ).fetchone()[0]
        assert n_hinted == 0
