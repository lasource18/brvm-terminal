"""Tests for the afx.kwayisi factsheet + competitors block parsers
(fallback data sources for the /s/{ticker} Description + Peers tabs)."""

from __future__ import annotations

from brvm.sources.afx_kwayisi import parse_competitors, parse_factsheet


def _read(fixtures_dir, rel: str) -> str:
    return (fixtures_dir / rel).read_text(encoding="utf-8")


class TestParseFactsheet:
    def test_snts_factsheet(self, fixtures_dir):
        f = parse_factsheet(_read(fixtures_dir, "afx/snts.html"))
        assert f["sector"] == "Telecommunications"
        assert f["industry"] == "Fixed Line Telecommunications"
        assert "Wagane Diouf" in f["address"]
        assert f["telephone"].startswith("+221")
        # Email + website are "—" in the fixture and should be omitted.
        assert "email" not in f
        assert "website" not in f


class TestParseCompetitors:
    def test_snts_competitors(self, fixtures_dir):
        peers = parse_competitors(_read(fixtures_dir, "afx/snts.html"))
        tickers = [p["ticker"] for p in peers]
        assert set(tickers) == {"ONTBF", "ORAC"}
        ontbf = next(p for p in peers if p["ticker"] == "ONTBF")
        assert ontbf["name"] == "Onatel Burkina Faso"
        assert ontbf["last"] == 2900.0
        assert ontbf["change_ytd_pct"] == 16.7
        assert ontbf["market_cap"] == 197_000_000_000  # "197B"

    def test_exclude_self(self, fixtures_dir):
        # If we pass exclude_ticker="ORAC" we shouldn't see it in the output.
        peers = parse_competitors(
            _read(fixtures_dir, "afx/snts.html"), exclude_ticker="ORAC"
        )
        assert all(p["ticker"] != "ORAC" for p in peers)
