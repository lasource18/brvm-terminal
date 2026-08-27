"""Fixture-based tests for the brvm.org bond parsers.

Reference figures come from HTML captured on 2026-08-27 against
/fr/cours-obligations/{20,21,55}. If brvm.org restructures the bond
table, one of these assertions will fail loudly rather than the code
crashing at runtime.
"""

from __future__ import annotations

from datetime import date

from brvm.sources.brvm_org_bonds import (
    BOND_CATEGORIES,
    BondCategory,
    parse_bonds,
)


def _read(fixtures_dir, rel: str) -> str:
    return (fixtures_dir / rel).read_text(encoding="utf-8")


STATE = BondCategory(20, "Obligations d'Etat")
REGIONAL = BondCategory(21, "Obligations régionales")
PRIVATE = BondCategory(55, "Obligations privées")

SESSION = date(2026, 8, 27)


class TestParseBondsState:
    def test_returns_multiple_bonds(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, bars = parse_bonds(html, STATE, today=SESSION)
        assert len(secs) >= 20
        assert len(secs) == len(bars)

    def test_all_kind_is_bond(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, _ = parse_bonds(html, STATE, today=SESSION)
        assert {s.kind for s in secs} == {"bond"}

    def test_sector_is_category_label(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, _ = parse_bonds(html, STATE, today=SESSION)
        assert {s.sector for s in secs} == {"Obligations d'Etat"}

    def test_currency_defaults_to_xof(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, _ = parse_bonds(html, STATE, today=SESSION)
        assert {s.currency for s in secs} == {"XOF"}

    def test_source_url_is_category_page(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, _ = parse_bonds(html, STATE, today=SESSION)
        assert all(
            s.source_url == "https://www.brvm.org/fr/cours-obligations/20"
            for s in secs
        )

    def test_mali_ticker_has_country_ml(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, _ = parse_bonds(html, STATE, today=SESSION)
        mali = next(s for s in secs if s.ticker == "EOM.O10")
        assert mali.country == "ML"
        assert "ETAT DU MALI" in mali.name

    def test_bidc_bond_has_price(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, bars = parse_bonds(html, STATE, today=SESSION)
        by_ticker = {b.ticker: b for b in bars}
        assert by_ticker["BIDC.O4"].close == 1250.0
        # BIDC-EBID isn't a sovereign, so country stays None even on the
        # state page.
        bidc_sec = next(s for s in secs if s.ticker == "BIDC.O4")
        assert bidc_sec.country is None

    def test_bar_uses_session_date_argument(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        _, bars = parse_bonds(html, STATE, today=SESSION)
        assert {b.session_date for b in bars} == {SESSION}
        assert {b.source for b in bars} == {"brvm_org"}

    def test_close_is_positive_for_every_row(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        _, bars = parse_bonds(html, STATE, today=SESSION)
        assert all(b.close > 0 for b in bars)


class TestParseBondsRegional:
    def test_returns_bonds(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_21.html")
        secs, _ = parse_bonds(html, REGIONAL, today=SESSION)
        assert len(secs) >= 10
        assert {s.sector for s in secs} == {"Obligations régionales"}

    def test_regional_bonds_have_no_state_country(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_21.html")
        secs, _ = parse_bonds(html, REGIONAL, today=SESSION)
        # Regional issuers (BOAD, BIDC, CRRH-UEMOA...) aren't tied to a
        # single WAEMU country — the name-based derivation must leave
        # country as None for all of them.
        assert all(s.country is None for s in secs)

    def test_skips_activities_market_table(self, fixtures_dir):
        # The regional page carries an "Activités du marché" summary table
        # ABOVE the bonds table (Valeur des transactions / BRVM-C / …).
        # None of those rows should slip into the bond output.
        html = _read(fixtures_dir, "brvm_org/cours_obligations_21.html")
        secs, _ = parse_bonds(html, REGIONAL, today=SESSION)
        tickers = {s.ticker for s in secs}
        for noise in {"BRVM-C", "BRVM-30", "BRVM-PRES", "Valeur des transactions"}:
            assert noise not in tickers


class TestParseBondsPrivate:
    def test_returns_bonds(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_55.html")
        secs, _ = parse_bonds(html, PRIVATE, today=SESSION)
        assert len(secs) >= 10
        assert {s.sector for s in secs} == {"Obligations privées"}


class TestBondCategoriesConstant:
    def test_covers_the_three_brvm_pages(self):
        assert {c.id for c in BOND_CATEGORIES} == {20, 21, 55}
        for cat in BOND_CATEGORIES:
            assert cat.url == f"https://www.brvm.org/fr/cours-obligations/{cat.id}"
