"""Page-level rendering tests for bond tabs.

The shared `client` fixture seeds equities + indices only; we insert
bond rows on top so the tabs have data to render.
"""

from __future__ import annotations

from datetime import date

from brvm.db import connect
from brvm.models import BondSnapshot, DailyBar, Security
from brvm.store import bonds as bonds_repo
from brvm.store import quotes as quotes_repo
from brvm.store import securities as sec_repo


def _seed_bond(client) -> None:
    from brvm.config import settings
    with connect(settings.db_path) as conn:
        sec_repo.upsert(conn, [
            Security(
                ticker="EOM.O10", name="ETAT DU MALI 6,20% 2022-2029",
                kind="bond", country="ML", sector="Obligations d'Etat",
                source_url="https://www.brvm.org/fr/cours-obligations/20",
                coupon_rate=6.20, maturity_year=2029,
                issue_date=date(2023, 2, 15), issuer_name="ETAT DU MALI",
            ),
            Security(
                ticker="EOM.O11", name="ETAT DU MALI 6,40% 2023-2030",
                kind="bond", country="ML", sector="Obligations d'Etat",
                coupon_rate=6.40, maturity_year=2030,
                issue_date=date(2023, 5, 17), issuer_name="ETAT DU MALI",
            ),
        ])
        quotes_repo.upsert_daily_bars(conn, [
            DailyBar(
                ticker="EOM.O10", session_date=date(2026, 8, 27),
                close=10_000.0, source="brvm_org",
            ),
        ])
        bonds_repo.upsert_snapshots(conn, [
            BondSnapshot(
                ticker="EOM.O10", session_date=date(2026, 8, 27),
                accrued_coupon=441.64,
                last_coupon_date=date(2025, 12, 9),
                last_coupon_amount=620.0,
                source="brvm_org",
            ),
        ])


def test_bond_bare_url_redirects_to_overview(client):
    _seed_bond(client)
    r = client.get("/s/EOM.O10", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/s/EOM.O10/overview"


def test_bond_overview_tab_renders_reference_block(client):
    _seed_bond(client)
    r = client.get("/s/EOM.O10/overview")
    assert r.status_code == 200
    body = r.text
    # Header + reference fields.
    assert "ETAT DU MALI" in body
    assert "6.20%" in body  # coupon
    assert "2029" in body   # maturity year
    assert "Obligations d&#39;Etat" in body  # Jinja auto-escaped apostrophe
    # Latest price + accrued.
    assert "10,000" in body
    assert "441.64" in body
    # Tabbar shows bond tabs, hides equity ones.
    assert "Overview" in body
    assert "Cash flow" in body
    assert "Yield &amp; Duration" in body
    assert "Related bonds" in body
    assert "Peers" not in body
    assert "Financials" not in body
    assert "Corporate actions" not in body


def test_bond_cashflow_tab_renders_schedule(client):
    _seed_bond(client)
    r = client.get("/s/EOM.O10/cashflow")
    assert r.status_code == 200
    body = r.text
    # 620 XOF annual coupon (6.20% of 10 000) shows up in the table.
    assert "620.00" in body
    # Terminal row with the nominal.
    assert "10,000" in body
    assert "bullet redemption" in body


def test_bond_yield_tab_renders_ytm_and_duration(client):
    _seed_bond(client)
    r = client.get("/s/EOM.O10/yield")
    assert r.status_code == 200
    body = r.text
    assert "Yield to maturity" in body
    assert "Modified duration" in body
    assert "Convexity" in body


def test_bond_related_tab_lists_sibling_bond(client):
    _seed_bond(client)
    r = client.get("/s/EOM.O10/related")
    assert r.status_code == 200
    body = r.text
    # EOM.O11 is the sibling; EOM.O10 (self) must be excluded.
    assert "EOM.O11" in body
    assert 'href="/s/EOM.O11"' in body


def test_bond_hidden_tabs_return_404(client):
    _seed_bond(client)
    # Financials / Peers / Corporate actions / etc. must 404 for bonds
    # so a stale bookmark doesn't render an empty equity template.
    for tab in ("financials", "peers", "corporate-actions", "ownership",
                "segments", "analyst", "description"):
        r = client.get(f"/s/EOM.O10/{tab}")
        assert r.status_code == 404, f"expected {tab} to 404 for a bond"


def test_equity_bare_url_still_redirects_to_chart(client):
    # Regression: the kind-aware redirect must not break equities.
    r = client.get("/s/SNTS", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/s/SNTS/chart"


def test_equity_hides_bond_tabs(client):
    # Regression: bond-only tabs must not appear in the equity tab bar.
    r = client.get("/s/SNTS/chart")
    assert r.status_code == 200
    body = r.text
    # Equity tabs still present.
    assert "Description" in body
    assert "Peers" in body
    # Bond tabs hidden.
    assert "Cash flow" not in body
    assert "Yield &amp; Duration" not in body
    assert "Related bonds" not in body
