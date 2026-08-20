"""End-to-end page render tests via FastAPI TestClient.

Uses the shared `client` fixture in conftest.py which seeds a fresh
SQLite DB per test and mocks the APScheduler.
"""

from __future__ import annotations


def test_index_renders_overview(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "brvm-terminal" in body
    assert "BRVM COMPOSITE" in body
    assert "Gainers" in body
    assert "Losers" in body
    assert "Turnover leaders" in body
    assert "SPHC" in body


def test_security_bare_url_redirects_to_overview(client):
    r = client.get("/s/SNTS", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/s/SNTS/overview"


def test_security_overview_tab_renders_chart(client):
    r = client.get("/s/SNTS/overview")
    assert r.status_code == 200
    body = r.text
    assert "SONATEL" in body
    assert 'data-ticker="SNTS"' in body
    assert "32,500" in body
    # Tab bar present with the eight tab keys
    for label in ("Overview", "Description", "Peers", "News", "Financials"):
        assert label in body


def test_security_description_tab(client, monkeypatch):
    def fake_societe(ticker, country, client=None):
        return {
            "description": "SONATEL est le premier opérateur télécom du Sénégal.",
            "address": "6 Rue WAGANE DIOUF",
            "phone": "(+221) 33-839-12-00",
            "shareholders": [("FRANCE TELECOM", 42.3)],
        }

    monkeypatch.setattr(
        "brvm.services.company.sikafinance.fetch_societe", fake_societe
    )
    from brvm.services import company

    company.clear_cache()

    r = client.get("/s/SNTS/description")
    assert r.status_code == 200
    assert "SONATEL" in r.text
    assert "premier opérateur" in r.text
    assert "FRANCE TELECOM" in r.text


def test_security_peers_tab(client, monkeypatch):
    def fake_secteur(ticker, country, client=None):
        return {
            "sector": "BRVM - TELECOMMUNICATIONS",
            "peers": [
                {"ticker": "ORAC", "country": "CI", "name": "ORANGE CI",
                 "last": 19000, "change_day_pct": 2.68, "change_ytd_pct": 36.91,
                 "volume": 4709},
                {"ticker": "ONTBF", "country": "BF", "name": "ONATEL BF",
                 "last": 2945, "change_day_pct": 1.55, "change_ytd_pct": 18.51,
                 "volume": 15573},
                {"ticker": "SNTS", "country": "SN", "name": "SONATEL",
                 "last": 32500, "change_day_pct": 1.88, "change_ytd_pct": 31.7,
                 "volume": 4867},
            ],
        }

    monkeypatch.setattr(
        "brvm.services.company.sikafinance.fetch_secteur", fake_secteur
    )
    from brvm.services import company

    company.clear_cache()

    r = client.get("/s/SNTS/peers")
    assert r.status_code == 200
    assert "ORAC" in r.text
    assert "ONTBF" in r.text
    # Self excluded
    assert "TELECOMMUNICATIONS" in r.text


def test_security_placeholder_tab(client):
    r = client.get("/s/SNTS/news")
    assert r.status_code == 200
    assert "Phase 3" in r.text


def test_security_unknown_tab_404(client):
    r = client.get("/s/SNTS/not-a-tab")
    assert r.status_code == 404


def test_security_unknown_ticker_404(client):
    r = client.get("/s/ZZZZ/overview")
    assert r.status_code == 404


def test_directory_renders_all(client):
    r = client.get("/directory")
    assert r.status_code == 200
    body = r.text
    # All 7 seeded securities show up.
    for t in ("SNTS", "ORAC", "SPHC", "CFAC", "BRVMC", "BRVM30", "BRVMPR"):
        assert t in body


def test_directory_filter_by_country(client):
    r = client.get("/directory?country=SN")
    assert r.status_code == 200
    assert "SNTS" in r.text
    # ORAC is CI, should not appear in the filtered table body (but may still
    # be in dropdowns, so use the ticker link href as a more precise signal).
    assert 'href="/s/ORAC"' not in r.text


def test_directory_filter_by_kind_equity(client):
    r = client.get("/directory?kind=equity")
    body = r.text
    assert 'href="/s/SNTS"' in body
    assert 'href="/s/BRVMC"' not in body


def test_watchlists_index_lists_default(client):
    r = client.get("/watchlists")
    assert r.status_code == 200
    assert "Default" in r.text


def test_watchlist_page_empty(client):
    r = client.get("/watchlists/default")
    assert r.status_code == 200
    assert "Empty" in r.text or "Add a ticker" in r.text


def test_health_json(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_topbar_search_input_present(client):
    r = client.get("/")
    assert 'id="search-input"' in r.text
    assert 'hx-get="/_frag/search"' in r.text
