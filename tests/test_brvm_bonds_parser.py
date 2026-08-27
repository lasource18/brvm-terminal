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
    parse_last_payment,
    parse_nom,
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
        secs, bars, snaps = parse_bonds(html, STATE, today=SESSION)
        assert len(secs) >= 20
        assert len(secs) == len(bars) == len(snaps)

    def test_all_kind_is_bond(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, _, _ = parse_bonds(html, STATE, today=SESSION)
        assert {s.kind for s in secs} == {"bond"}

    def test_sector_is_category_label(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, _, _ = parse_bonds(html, STATE, today=SESSION)
        assert {s.sector for s in secs} == {"Obligations d'Etat"}

    def test_currency_defaults_to_xof(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, _, _ = parse_bonds(html, STATE, today=SESSION)
        assert {s.currency for s in secs} == {"XOF"}

    def test_source_url_is_category_page(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, _, _ = parse_bonds(html, STATE, today=SESSION)
        assert all(
            s.source_url == "https://www.brvm.org/fr/cours-obligations/20"
            for s in secs
        )

    def test_mali_ticker_has_country_ml(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, _, _ = parse_bonds(html, STATE, today=SESSION)
        mali = next(s for s in secs if s.ticker == "EOM.O10")
        assert mali.country == "ML"
        assert "ETAT DU MALI" in mali.name

    def test_bidc_bond_has_price(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, bars, _ = parse_bonds(html, STATE, today=SESSION)
        by_ticker = {b.ticker: b for b in bars}
        assert by_ticker["BIDC.O4"].close == 1250.0
        # BIDC-EBID isn't a sovereign, so country stays None even on the
        # state page.
        bidc_sec = next(s for s in secs if s.ticker == "BIDC.O4")
        assert bidc_sec.country is None

    def test_bar_uses_session_date_argument(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        _, bars, _ = parse_bonds(html, STATE, today=SESSION)
        assert {b.session_date for b in bars} == {SESSION}
        assert {b.source for b in bars} == {"brvm_org"}

    def test_close_is_positive_for_every_row(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        _, bars, _ = parse_bonds(html, STATE, today=SESSION)
        assert all(b.close > 0 for b in bars)


class TestParseBondsRegional:
    def test_returns_bonds(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_21.html")
        secs, _, _ = parse_bonds(html, REGIONAL, today=SESSION)
        assert len(secs) >= 10
        assert {s.sector for s in secs} == {"Obligations régionales"}

    def test_regional_bonds_have_no_state_country(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_21.html")
        secs, _, _ = parse_bonds(html, REGIONAL, today=SESSION)
        # Regional issuers (BOAD, BIDC, CRRH-UEMOA...) aren't tied to a
        # single WAEMU country — the name-based derivation must leave
        # country as None for all of them.
        assert all(s.country is None for s in secs)

    def test_skips_activities_market_table(self, fixtures_dir):
        # The regional page carries an "Activités du marché" summary table
        # ABOVE the bonds table (Valeur des transactions / BRVM-C / …).
        # None of those rows should slip into the bond output.
        html = _read(fixtures_dir, "brvm_org/cours_obligations_21.html")
        secs, _, _ = parse_bonds(html, REGIONAL, today=SESSION)
        tickers = {s.ticker for s in secs}
        for noise in {"BRVM-C", "BRVM-30", "BRVM-PRES", "Valeur des transactions"}:
            assert noise not in tickers


class TestParseBondsPrivate:
    def test_returns_bonds(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_55.html")
        secs, _, _ = parse_bonds(html, PRIVATE, today=SESSION)
        assert len(secs) >= 10
        assert {s.sector for s in secs} == {"Obligations privées"}


class TestBondCategoriesConstant:
    def test_covers_the_three_brvm_pages(self):
        assert {c.id for c in BOND_CATEGORIES} == {20, 21, 55}
        for cat in BOND_CATEGORIES:
            assert cat.url == f"https://www.brvm.org/fr/cours-obligations/{cat.id}"


# ---------- Phase 8b enrichment: parse_nom + parse_last_payment ----------


class TestParseNom:
    def test_comma_decimal_coupon(self):
        p = parse_nom("BIDC-EBID 6,10% 2017-2027")
        assert p is not None
        assert p.issuer_name == "BIDC-EBID"
        assert p.coupon_rate == 6.10
        assert p.issue_year == 2017
        assert p.maturity_year == 2027

    def test_period_decimal_coupon(self):
        p = parse_nom("BHB 6.25% 2012-2017")
        assert p is not None
        assert p.coupon_rate == 6.25

    def test_state_issuer_multi_word_name(self):
        p = parse_nom("ETAT DU MALI 6,20% 2022-2029")
        assert p is not None
        assert p.issuer_name == "ETAT DU MALI"
        assert p.coupon_rate == 6.20

    def test_space_before_percent(self):
        # ETAT DU MALI 3,00 % 2024-2031 — brvm.org sometimes emits a
        # space between the number and the percent sign.
        p = parse_nom("ETAT DU MALI 3,00 % 2024-2031")
        assert p is not None
        assert p.coupon_rate == 3.00
        assert p.maturity_year == 2031

    def test_integer_coupon_no_decimal(self):
        p = parse_nom("KEUR SAMBA NSIA BQE CI 7% 2025-2030")
        assert p is not None
        # KEUR SAMBA is stripped so related-bond lookups group siblings.
        assert p.issuer_name == "NSIA BQE CI"
        assert p.coupon_rate == 7.0

    def test_social_bond_prefix_stripped(self):
        p = parse_nom("SOCIAL BOND CRRH-UEMOA 6,00% 2025-2040")
        assert p is not None
        assert p.issuer_name == "CRRH-UEMOA"

    def test_gender_bond_prefix_stripped(self):
        p = parse_nom("GENDER BOND ECOBANK CI 6,50% 2024-2029")
        assert p is not None
        assert p.issuer_name == "ECOBANK CI"

    def test_diaspora_bonds_prefix_stripped(self):
        p = parse_nom("DIASPORA BONDS BHS 6,25% 2019-2024")
        assert p is not None
        assert p.issuer_name == "BHS"

    def test_returns_none_on_unmatched_shape(self):
        # Freshly-admitted bonds sometimes show "à préciser" or a raw
        # code as their Nom — we want to skip enrichment cleanly, not
        # raise, so the ingest keeps going.
        assert parse_nom("à préciser") is None
        assert parse_nom("") is None
        assert parse_nom("SNTS") is None


class TestParseLastPayment:
    def test_typical_row(self):
        d, a = parse_last_payment("16/06/2026 / 76,25")
        assert d == date(2026, 6, 16)
        assert a == 76.25

    def test_amount_with_space_thousands(self):
        d, a = parse_last_payment("04/04/2016 / 1 001,43")
        assert d == date(2016, 4, 4)
        assert a == 1001.43

    def test_missing_returns_none_pair(self):
        assert parse_last_payment("") == (None, None)
        assert parse_last_payment("-") == (None, None)


# ---------- Phase 8b: fields on parsed Security + BondSnapshot rows ----------


class TestParseBondsEnrichmentFields:
    def test_bidc_o4_reference_fields(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, _, _ = parse_bonds(html, STATE, today=SESSION)
        bidc = next(s for s in secs if s.ticker == "BIDC.O4")
        assert bidc.coupon_rate == 6.10
        assert bidc.maturity_year == 2027
        assert bidc.issue_date == date(2017, 11, 30)
        assert bidc.issuer_name == "BIDC-EBID"

    def test_state_bond_issuer_name(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        secs, _, _ = parse_bonds(html, STATE, today=SESSION)
        mali = next(s for s in secs if s.ticker == "EOM.O10")
        assert mali.issuer_name == "ETAT DU MALI"

    def test_snapshot_carries_accrued_and_last_payment(self, fixtures_dir):
        html = _read(fixtures_dir, "brvm_org/cours_obligations_20.html")
        _, _, snaps = parse_bonds(html, STATE, today=SESSION)
        by_ticker = {s.ticker: s for s in snaps}
        bidc = by_ticker["BIDC.O4"]
        assert bidc.accrued_coupon == 14.83
        assert bidc.last_coupon_date == date(2026, 6, 16)
        assert bidc.last_coupon_amount == 76.25
        assert bidc.session_date == SESSION
        assert bidc.source == "brvm_org"

    def test_social_bond_groups_with_plain_issuer(self, fixtures_dir):
        # SOCIAL BOND CRRH-UEMOA and plain CRRH-UEMOA must share an
        # issuer_name so the Related-bonds lookup groups them together.
        html = _read(fixtures_dir, "brvm_org/cours_obligations_21.html")
        secs, _, _ = parse_bonds(html, REGIONAL, today=SESSION)
        crrh_bonds = [s for s in secs if s.issuer_name == "CRRH-UEMOA"]
        # Fixture has at least one SOCIAL BOND CRRH-UEMOA + several plain
        # CRRH-UEMOA bonds; grouping should merge them.
        assert len(crrh_bonds) >= 2
        assert any("SOCIAL BOND" in s.name for s in crrh_bonds)
        assert any("SOCIAL BOND" not in s.name for s in crrh_bonds)
