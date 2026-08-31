"""Tests for the HTMX fragment endpoints + JSON history API."""

from __future__ import annotations


def test_overview_fragment(client):
    r = client.get("/_frag/overview")
    assert r.status_code == 200
    assert 'id="overview-panel"' in r.text
    assert "hx-get=\"/_frag/overview\"" in r.text


def test_create_watchlist_and_add_remove(client):
    # 1) Create a new list "Core"
    r = client.post("/_frag/watchlists", data={"name": "Core"})
    assert r.status_code == 200
    assert "Core" in r.text

    # 2) Add SNTS to it
    r = client.post("/_frag/watchlists/core/items", data={"ticker": "SNTS"})
    assert r.status_code == 200
    assert "SNTS" in r.text
    assert "SONATEL" in r.text

    # Adding again is a no-op (dedupe) with a 200 response.
    r2 = client.post("/_frag/watchlists/core/items", data={"ticker": "SNTS"})
    assert r2.status_code == 200

    # 3) Remove SNTS
    r = client.delete("/_frag/watchlists/core/items/SNTS")
    assert r.status_code == 200
    assert "Empty" in r.text or "Add a ticker" in r.text


def test_add_unknown_ticker_returns_400(client):
    client.post("/_frag/watchlists", data={"name": "Tmp"})
    r = client.post("/_frag/watchlists/tmp/items", data={"ticker": "ZZZZ"})
    assert r.status_code == 400


def test_add_to_unknown_watchlist_returns_404(client):
    r = client.post("/_frag/watchlists/does-not-exist/items", data={"ticker": "SNTS"})
    assert r.status_code == 404


def test_history_api_returns_ascending_bars(client, monkeypatch):
    """The /api/history endpoint must return oldest -> newest for Lightweight
    Charts. We stub sikafinance.fetch_historique so the test stays offline.
    """
    from datetime import date, timedelta

    from kodji.models import DailyBar

    def fake_fetch(ticker, country, client=None):
        base = date(2026, 8, 18)
        return [
            DailyBar(
                ticker="SNTS",
                session_date=base - timedelta(days=i),
                open=32000 + i,
                high=32500 + i,
                low=31900 + i,
                close=32000 + i,
                volume=1000 * (i + 1),
                turnover=32_000_000 * (i + 1),
                source="sikafinance",
            )
            for i in range(5)
        ]

    monkeypatch.setattr(
        "kodji.services.history.sikafinance.fetch_historique", fake_fetch
    )
    from kodji.services import history as h
    h.clear_cache()

    r = client.get("/api/history/SNTS")
    assert r.status_code == 200
    payload = r.json()
    assert payload["kind"] == "equity"
    bars = payload["bars"]
    # 5 historical + 1 intraday overlay (Phase 4d/e — the SNTS quote seed
    # in conftest produces today's synthetic candle from `quote_snapshots`).
    assert len(bars) >= 5
    # Ascending by time — the intraday bar (today) is naturally the last.
    dates = [b["time"] for b in bars]
    assert dates == sorted(dates)
    assert bars[0].keys() >= {"time", "open", "high", "low", "close", "volume"}


def test_history_api_index_returns_line_bars(client):
    """Indices are served from index_levels with kind='index' and close-only bars."""
    r = client.get("/api/history/BRVMC")
    assert r.status_code == 200
    payload = r.json()
    assert payload["kind"] == "index"
    bars = payload["bars"]
    # Seeded fixture has one index level for BRVMC.
    assert len(bars) >= 1
    assert bars[0]["close"] is not None
    assert bars[0]["open"] is None
    assert bars[0]["volume"] is None


def test_history_api_404_unknown_ticker(client):
    r = client.get("/api/history/ZZZZ")
    assert r.status_code == 404


def test_search_fragment_matches_ticker(client):
    r = client.get("/_frag/search?q=SNTS")
    assert r.status_code == 200
    assert "SNTS" in r.text
    assert "SONATEL" in r.text


def test_search_fragment_matches_name(client):
    r = client.get("/_frag/search?q=orange")
    assert r.status_code == 200
    assert "ORAC" in r.text


def test_search_fragment_empty_query_returns_empty_body(client):
    r = client.get("/_frag/search?q=")
    assert r.status_code == 200
    # No hits list rendered when the query is blank.
    assert "<ul" not in r.text


def test_directory_fragment_filters(client):
    r = client.get("/_frag/directory?country=SN")
    assert r.status_code == 200
    assert 'id="dir-body"' in r.text
    assert 'href="/s/SNTS"' in r.text
    assert 'href="/s/ORAC"' not in r.text
