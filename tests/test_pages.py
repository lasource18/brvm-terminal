"""End-to-end page render tests via FastAPI TestClient.

Uses the shared `client` fixture in conftest.py which seeds a fresh
SQLite DB per test and mocks the APScheduler.
"""

from __future__ import annotations


def test_index_renders_overview(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "kodji-terminal" in body
    assert "BRVM COMPOSITE" in body
    assert "Gainers" in body
    assert "Losers" in body
    assert "Turnover leaders" in body
    assert "SPHC" in body


def test_security_bare_url_redirects_to_chart(client):
    r = client.get("/s/SNTS", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/s/SNTS/chart"


def test_security_chart_tab_renders_chart(client):
    r = client.get("/s/SNTS/chart")
    assert r.status_code == 200
    body = r.text
    assert "SONATEL" in body
    assert 'data-ticker="SNTS"' in body
    assert "32,500" in body
    # Tab bar present with the eight tab keys
    for label in ("Chart", "Description", "Peers", "News", "Financials"):
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
        "kodji.services.company.sikafinance.fetch_societe", fake_societe
    )
    from kodji.services import company

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
        "kodji.services.company.sikafinance.fetch_secteur", fake_secteur
    )
    from kodji.services import company

    company.clear_cache()

    r = client.get("/s/SNTS/peers")
    assert r.status_code == 200
    assert "ORAC" in r.text
    assert "ONTBF" in r.text
    # Self excluded
    assert "TELECOMMUNICATIONS" in r.text
    # Phase 8g: median + mean summary rows land underneath the peer list.
    # ORAC + ONTBF ytd = 36.91 + 18.51 → median 27.71, mean 27.71.
    assert "MEDIAN" in r.text
    assert "MEAN" in r.text
    assert "+27.71%" in r.text


def test_security_fundamentals_tabs_empty_state(client):
    # No financials extracted for the seeded ticker yet; the three
    # fundamentals tabs must still render 200 with graceful copy.
    for tab, needle in (
        ("financials", "No extracted financials"),
        ("ownership", "No ownership data extracted"),
        ("segments", "No segment breakdown"),
    ):
        r = client.get(f"/s/SNTS/{tab}")
        assert r.status_code == 200, tab
        assert needle in r.text, tab


def test_security_fundamentals_tabs_render_extracted_data(client):
    """Seed a full fundamentals triple and check the three tabs render
    the real values, not the empty-state copy."""
    from datetime import date

    from kodji.config import settings
    from kodji.db import connect
    from kodji.models import Filing
    from kodji.store import filings as filings_repo
    from kodji.store import financials as fin_repo

    filing = Filing(
        ticker="SNTS",
        issuer_name="SONATEL",
        doc_type="rapport_annuel",
        period_kind="annual",
        period_year=2024,
        source="brvm_org",
        source_url="https://brvm.org/sonatel/2024.pdf",
        url_hash="hash-snts-2024",
        published_date=date(2025, 3, 15),
        file_path="data/filings/SNTS/2024.pdf",
        size_bytes=1024,
        sha256="deadbeef",
        page_count=100,
    )
    with connect(settings.db_path) as conn:
        filings_repo.upsert_filings(conn, [filing])
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        fin_repo.replace_period(
            conn,
            filing_id=filing_id,
            financials=fin_repo.FinancialsRow(
                ticker="SNTS",
                period_year=2024,
                revenue=1_500_000_000,
                net_income=300_000_000,
            ),
            segments=[
                fin_repo.SegmentRow(name="Mobile Money", segment_kind="business", share_pct=25.5),
            ],
            ownership=[
                fin_repo.OwnershipRow(holder="SONATEL SA", share_pct=42.3),
            ],
        )

    r = client.get("/s/SNTS/financials")
    assert r.status_code == 200
    assert "2024" in r.text
    assert "1,500,000,000" in r.text
    assert "No extracted financials" not in r.text
    # References subsection links back to the source PDF so a reader can
    # audit each number against the filing that produced it.
    assert "References" in r.text
    assert "https://brvm.org/sonatel/2024.pdf" in r.text
    assert "rapport_annuel" in r.text

    r = client.get("/s/SNTS/ownership")
    assert r.status_code == 200
    assert "SONATEL SA" in r.text
    assert "42.30%" in r.text

    r = client.get("/s/SNTS/segments")
    assert r.status_code == 200
    assert "Mobile Money" in r.text
    assert "25.5%" in r.text


def test_security_unknown_tab_404(client):
    r = client.get("/s/SNTS/not-a-tab")
    assert r.status_code == 404


def test_security_unknown_ticker_404(client):
    r = client.get("/s/ZZZZ/chart")
    assert r.status_code == 404


def test_index_chart_tab_renders(client):
    r = client.get("/s/BRVMC/chart")
    assert r.status_code == 200
    body = r.text
    assert "BRVM COMPOSITE" in body
    assert 'data-ticker="BRVMC"' in body
    # Only Chart + News tabs are shown for indices.
    assert 'href="/s/BRVMC/chart"' in body
    assert 'href="/s/BRVMC/news"' in body
    for hidden in ("description", "peers", "corporate-actions",
                   "financials", "ownership", "segments"):
        assert f'href="/s/BRVMC/{hidden}"' not in body


def test_index_hidden_tabs_return_404(client):
    for hidden in ("description", "peers", "corporate-actions",
                   "financials", "ownership", "segments"):
        r = client.get(f"/s/BRVMC/{hidden}")
        assert r.status_code == 404, hidden


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
