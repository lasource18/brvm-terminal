"""F-38: watchlist quote board must join bond prices too.

Equities' prices live in `quote_snapshots`; bonds' live in
`daily_bars`. The earlier query only joined `quote_snapshots`, so a
bond added to a watchlist rendered permanent em-dashes on the quote
board — even when the exchange page had a live close.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brvm.config import reset_settings_cache
from brvm.db import connect
from brvm.models import DailyBar, Quote, Security
from brvm.store import quotes as quotes_repo
from brvm.store import securities as sec_repo

from .conftest import apply_migrations


def _setup(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from brvm.services import watchlist as svc
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
            Security(ticker="BIDCO4", name="BIDC.O4 SUPRA 6.10% 2017-2027",
                     kind="bond", country="CI"),
        ])
    return db_path, svc


def test_bond_ticker_in_watchlist_renders_daily_bar_close(monkeypatch, tmp_path):
    """F-38 regression: a bond row must expose `last`, `volume`, and
    `turnover` sourced from `daily_bars`, not permanent em-dashes."""
    db_path, svc = _setup(monkeypatch, tmp_path)

    with connect(db_path) as conn:
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="SNTS", source="sikafinance", last=32_500.0,
                  change_pct=1.88, volume=3000, turnover=97_500_000.0),
        ])
        quotes_repo.upsert_daily_bars(conn, [
            DailyBar(ticker="BIDCO4", session_date=date(2026, 8, 27),
                     close=1_250.0, volume=42, turnover=52_500.0,
                     source="brvm_org"),
        ])

    wl = svc.create("Fixed income")
    svc.add_item(wl.slug, "SNTS")
    svc.add_item(wl.slug, "BIDCO4")

    view = svc.get_with_quotes(wl.slug)
    by = {q.ticker: q for q in view.items}
    assert by["SNTS"].last == 32_500.0
    assert by["SNTS"].change_pct == 1.88
    # Bond price now surfaces.
    assert by["BIDCO4"].last == 1_250.0
    assert by["BIDCO4"].volume == 42
    assert by["BIDCO4"].turnover == 52_500.0
    # Bond change_pct stays None — the exchange doesn't publish a
    # bond-side day-change % and computing one from a two-day diff
    # would be misleading vs. equities' intraday %.
    assert by["BIDCO4"].change_pct is None


def test_bond_without_daily_bar_still_renders_em_dash(monkeypatch, tmp_path):
    """Bond in a watchlist but not yet quoted (freshly admitted,
    never traded) should degrade to None cleanly — not raise, not
    fabricate a value."""
    _db_path, svc = _setup(monkeypatch, tmp_path)
    wl = svc.create("New listings")
    svc.add_item(wl.slug, "BIDCO4")
    view = svc.get_with_quotes(wl.slug)
    assert len(view.items) == 1
    assert view.items[0].last is None
    assert view.items[0].change_pct is None
