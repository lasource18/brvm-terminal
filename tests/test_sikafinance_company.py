"""Fixture-based tests for the /marches/societe/ and /marches/secteur/ parsers."""

from __future__ import annotations

from kodji.sources.sikafinance import parse_secteur, parse_societe


def _read(fixtures_dir, rel: str) -> str:
    return (fixtures_dir / rel).read_text(encoding="utf-8")


class TestParseSociete:
    def test_snts_description(self, fixtures_dir):
        p = parse_societe(_read(fixtures_dir, "sikafinance/societe_SNTS.html"))
        assert p["ticker"] == "SNTS"
        assert p["isin"] == "SN0000000019"
        assert "SONATEL" in p["description"]
        assert "1985" in p["description"]  # founding year appears

    def test_snts_contacts(self, fixtures_dir):
        p = parse_societe(_read(fixtures_dir, "sikafinance/societe_SNTS.html"))
        assert p["phone"].startswith("(+221)")
        assert "WAGANE DIOUF" in p["address"]

    def test_snts_capital_fields(self, fixtures_dir):
        p = parse_societe(_read(fixtures_dir, "sikafinance/societe_SNTS.html"))
        assert p["shares_outstanding"].replace("\u00a0", " ") == "100 000 000"
        assert p["float_pct"] == "22,47%"

    def test_snts_leadership(self, fixtures_dir):
        p = parse_societe(_read(fixtures_dir, "sikafinance/societe_SNTS.html"))
        assert "Alioune NDIAYE" in p["leadership"]
        assert "Brelotte BA" in p["leadership"]

    def test_snts_shareholders(self, fixtures_dir):
        p = parse_societe(_read(fixtures_dir, "sikafinance/societe_SNTS.html"))
        assert ("FRANCE TELECOM", 42.3) in p["shareholders"]
        assert ("ETAT DU SENEGAL", 27.7) in p["shareholders"]
        # Sum should be close to 100 for a well-known name.
        total = sum(pct for _, pct in p["shareholders"])
        assert 99.0 <= total <= 101.0


class TestParseSecteur:
    def test_snts_sector_name(self, fixtures_dir):
        s = parse_secteur(_read(fixtures_dir, "sikafinance/secteur_SNTS.html"))
        assert s["sector"] == "BRVM - TELECOMMUNICATIONS"

    def test_snts_peers(self, fixtures_dir):
        s = parse_secteur(_read(fixtures_dir, "sikafinance/secteur_SNTS.html"))
        peers = s["peers"]
        tickers = [p["ticker"] for p in peers]
        assert set(tickers) == {"ONTBF", "ORAC", "SNTS"}
        snts = next(p for p in peers if p["ticker"] == "SNTS")
        assert snts["country"] == "SN"
        assert snts["last"] == 34400.0
        assert snts["change_day_pct"] == 5.85
        assert snts["change_ytd_pct"] == 31.70
        assert snts["volume"] == 4867
        assert snts["name"] == "SONATEL"
