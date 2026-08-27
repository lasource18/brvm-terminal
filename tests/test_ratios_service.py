"""Phase 4d: financial ratios math + DB-facing helpers.

The pure functions are tested first (no DB, no imports of the service),
then a handful of integration tests wire the store + service together to
make sure the peers path and interim card get sensible inputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brvm.config import reset_settings_cache
from brvm.db import connect
from brvm.models import Quote, Security
from brvm.services.ratios import (
    FinancialsInput,
    MarketInput,
    Ratio,
    compute_ratios,
    growth_ratios,
    leverage_ratios,
    profitability_ratios,
    valuation_ratios,
)
from brvm.store import quotes as quotes_repo
from brvm.store import securities as sec_repo

from .conftest import apply_migrations

# ---------------------------------------------------------------------------
# Pure math — the important edge cases we don't want to regress on.
# ---------------------------------------------------------------------------


def _fin(**over) -> FinancialsInput:
    base = dict(
        ticker="SNTS",
        period_year=2024,
        period_kind="annual",
        currency="XOF",
        revenue=1_000.0,
        operating_income=250.0,
        net_income=200.0,
        total_assets=5_000.0,
        total_equity=2_000.0,
        eps=10.0,
        dividend_per_share=4.0,
    )
    base.update(over)
    return FinancialsInput(**base)


def _mkt(**over) -> MarketInput:
    base = dict(
        price=80.0,
        price_currency="XOF",
        shares_outstanding=100_000,
        market_cap_xof=None,
    )
    base.update(over)
    return MarketInput(**base)


def test_valuation_ratios_happy_path():
    got = valuation_ratios(_fin(), _mkt(), currency_mismatch=False)
    # P/E = 80 / 10 = 8x
    assert got["pe"].value == pytest.approx(8.0)
    assert got["pe"].unit == "x"
    # P/B: book value / share = 2000 / 100000 = 0.02 → P/B = 80 / 0.02 = 4000
    assert got["pb"].value == pytest.approx(4000.0)
    # P/S: revenue / share = 1000 / 100000 = 0.01 → P/S = 80 / 0.01 = 8000
    assert got["ps"].value == pytest.approx(8000.0)
    # Div yield = 4 / 80 = 5%
    assert got["dividend_yield"].value == pytest.approx(5.0)
    assert got["dividend_yield"].unit == "pct"
    # Payout = 4 / 10 = 40%
    assert got["payout_ratio"].value == pytest.approx(40.0)
    # Earnings yield = 10 / 80 = 12.5%
    assert got["earnings_yield"].value == pytest.approx(12.5)


def test_valuation_ratios_currency_mismatch_returns_none_for_price_ratios():
    """Payout ratio doesn't touch price — still computable. P/E does — None."""
    got = valuation_ratios(_fin(), _mkt(), currency_mismatch=True)
    assert got["pe"] is None
    assert got["pb"] is None
    assert got["ps"] is None
    assert got["dividend_yield"] is None
    assert got["earnings_yield"] is None
    # Purely-financials ratio still computes.
    assert got["payout_ratio"].value == pytest.approx(40.0)


def test_valuation_ratios_missing_inputs():
    # No shares — P/B and P/S can't be computed but P/E still can.
    got = valuation_ratios(_fin(), _mkt(shares_outstanding=None), currency_mismatch=False)
    assert got["pe"].value == pytest.approx(8.0)
    assert got["pb"] is None
    assert got["ps"] is None
    # No price — every price-based ratio drops.
    got = valuation_ratios(_fin(), _mkt(price=None), currency_mismatch=False)
    assert got["pe"] is None
    assert got["dividend_yield"] is None
    assert got["earnings_yield"] is None
    # Payout still computable (no price dependency).
    assert got["payout_ratio"].value == pytest.approx(40.0)


def test_zero_divisor_never_produces_inf_or_nan():
    """Every ratio with a zero denominator returns None. Never inf, never
    NaN — the Financials tab renders '—' and moves on."""
    zeros = _fin(eps=0.0, total_equity=0.0, total_assets=0.0, revenue=0.0)
    val = valuation_ratios(zeros, _mkt(), currency_mismatch=False)
    prof = profitability_ratios(zeros)
    lev = leverage_ratios(zeros)
    for bag in (val, prof, lev):
        for name, r in bag.items():
            assert r is None or (r.value is not None
                                 and r.value != float("inf")
                                 and r.value != float("-inf")
                                 and r.value == r.value), name  # NaN != NaN


def test_profitability_ratios():
    got = profitability_ratios(_fin())
    assert got["roe"].value == pytest.approx(10.0)             # 200 / 2000
    assert got["roa"].value == pytest.approx(4.0)              # 200 / 5000
    assert got["net_margin"].value == pytest.approx(20.0)      # 200 / 1000
    assert got["operating_margin"].value == pytest.approx(25.0)  # 250 / 1000
    for r in got.values():
        assert r.unit == "pct"


def test_growth_ratios_prior_same_kind_only():
    curr = _fin(period_year=2024, revenue=1_200.0, net_income=240.0, eps=12.0)
    prior_annual = _fin(period_year=2023, revenue=1_000.0, net_income=200.0, eps=10.0)
    prior_h1 = _fin(period_year=2023, period_kind="H1",
                    revenue=500.0, net_income=90.0, eps=4.5)

    got = growth_ratios(curr, prior_annual)
    assert got["revenue_growth"].value == pytest.approx(20.0)
    assert got["net_income_growth"].value == pytest.approx(20.0)
    assert got["eps_growth"].value == pytest.approx(20.0)
    # Provenance names which years we compared.
    assert "2024" in got["revenue_growth"].provenance
    assert "2023" in got["revenue_growth"].provenance

    # Mixing an annual current with an H1 prior would be misleading.
    got_mixed = growth_ratios(curr, prior_h1)
    assert got_mixed == {
        "revenue_growth": None,
        "net_income_growth": None,
        "eps_growth": None,
    }


def test_growth_ratios_no_prior_returns_none():
    got = growth_ratios(_fin(), None)
    assert got == {
        "revenue_growth": None,
        "net_income_growth": None,
        "eps_growth": None,
    }


def test_leverage_ratios():
    got = leverage_ratios(_fin())
    assert got["financial_leverage"].value == pytest.approx(2.5)
    assert got["equity_ratio"].value == pytest.approx(40.0)


def test_compute_ratios_sets_market_cap_when_shares_and_price_known():
    view = compute_ratios(_fin(), _mkt(), prior=None)
    assert view.market_cap_xof == pytest.approx(80.0 * 100_000)
    assert view.currency_mismatch is False
    assert view.pe is not None
    assert view.has_any is True


def test_compute_ratios_flags_currency_mismatch():
    view = compute_ratios(
        _fin(currency="EUR"),
        _mkt(price=80.0, price_currency="XOF"),
        prior=None,
    )
    assert view.currency_mismatch is True
    assert view.pe is None  # refuses the multi-currency multiple
    # Financials-only ratios still populate.
    assert view.roe is not None


def test_ratio_dataclass_carries_provenance_string():
    r = Ratio(value=1.23, provenance="x / y", unit="x")
    assert r.provenance == "x / y"


# ---------------------------------------------------------------------------
# Cash-flow ratios (Phase 7) — P/FCF, FCF yield, EV/EBITDA proxy.
# ---------------------------------------------------------------------------


def test_valuation_ratios_computes_pfcf_and_fcf_yield_when_fcf_positive():
    fin = _fin(free_cash_flow=100.0)  # market cap = 80 * 100_000 = 8_000_000
    got = valuation_ratios(fin, _mkt(), currency_mismatch=False)
    # P/FCF = 8_000_000 / 100 = 80_000x
    assert got["pfcf"].value == pytest.approx(80_000.0)
    assert got["pfcf"].unit == "x"
    # FCF yield = 100 / 8_000_000 * 100 = 0.00125%
    assert got["fcf_yield"].value == pytest.approx(0.00125)
    assert got["fcf_yield"].unit == "pct"


def test_valuation_ratios_suppresses_pfcf_when_fcf_negative_but_keeps_yield():
    """A negative multiple would trap a skimming reader — hide P/FCF, but
    the yield (as a signed percent) still communicates the direction."""
    fin = _fin(free_cash_flow=-50.0)
    got = valuation_ratios(fin, _mkt(), currency_mismatch=False)
    assert got["pfcf"] is None
    assert got["fcf_yield"] is not None
    assert got["fcf_yield"].value < 0


def test_valuation_ratios_ev_ebitda_proxy_when_operating_income_positive():
    got = valuation_ratios(_fin(operating_income=200.0), _mkt(), currency_mismatch=False)
    # Proxy: market_cap / operating_income = 8_000_000 / 200 = 40_000x
    assert got["ev_ebitda"].value == pytest.approx(40_000.0)
    # Provenance must flag the proxy so a reader isn't misled.
    assert "EV proxy" in got["ev_ebitda"].provenance
    assert "operating_income" in got["ev_ebitda"].provenance


def test_valuation_ratios_ev_ebitda_none_when_operating_income_zero_or_negative():
    for oi in (0.0, -10.0, None):
        got = valuation_ratios(_fin(operating_income=oi), _mkt(), currency_mismatch=False)
        assert got["ev_ebitda"] is None


def test_valuation_ratios_cashflow_ratios_none_on_currency_mismatch():
    fin = _fin(free_cash_flow=100.0, operating_income=200.0, currency="EUR")
    got = valuation_ratios(fin, _mkt(), currency_mismatch=True)
    assert got["pfcf"] is None
    assert got["fcf_yield"] is None
    assert got["ev_ebitda"] is None


def test_valuation_ratios_cashflow_ratios_none_when_no_market_cap():
    """No shares → no market cap → nothing to divide by."""
    got = valuation_ratios(
        _fin(free_cash_flow=100.0), _mkt(shares_outstanding=None),
        currency_mismatch=False,
    )
    assert got["pfcf"] is None
    assert got["fcf_yield"] is None
    assert got["ev_ebitda"] is None


def test_ratios_view_has_any_true_when_only_cashflow_ratio_populated():
    """A ticker with only cash-flow data should still light up the ratios
    table — `has_any` gates the whole section, so the new fields need
    to count."""
    view = compute_ratios(
        _fin(
            revenue=None, operating_income=None, net_income=None,
            total_assets=None, total_equity=None, eps=None,
            dividend_per_share=None, free_cash_flow=100.0,
        ),
        _mkt(),
    )
    # Nothing except the cash-flow multiples should be populated.
    assert view.pe is None and view.roe is None
    assert view.pfcf is not None
    assert view.has_any is True


# ---------------------------------------------------------------------------
# Integration — the DB-facing helpers that the templates and Peers path use.
# ---------------------------------------------------------------------------


def _setup(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from brvm.services import ratios as ratios_svc
    from brvm.store import financials as fin_repo

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
        ])
        sec_repo.update_company_facts(
            conn, "SNTS",
            shares_outstanding=100_000_000,
            float_pct=22.47,
            market_cap_xof=3_440_000_000_000.0,
        )
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="SNTS", source="sikafinance", last=32_500.0, change_pct=1.0),
        ])
    return db_path, ratios_svc, fin_repo


def test_get_ratios_series_computes_yoy_across_two_annual_rows(monkeypatch, tmp_path):
    db_path, ratios_svc, fin_repo = _setup(monkeypatch, tmp_path)
    with connect(db_path) as conn:
        # Fake filing_id — we only need a valid FK for the compound key.
        from brvm.models import Filing
        from brvm.store import filings as filings_repo
        filings_repo.upsert_filings(conn, [Filing(
            ticker="SNTS", issuer_name="SONATEL", doc_type="rapport_annuel",
            period_kind="annual", period_year=2024, source="brvm_org",
            source_url="u1", url_hash="h1",
            file_path="p1", size_bytes=1, sha256="a", page_count=1,
        )])
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        fin_repo.replace_period(conn, filing_id=filing_id, financials=fin_repo.FinancialsRow(
            ticker="SNTS", period_year=2024, revenue=1_500_000_000_000,
            net_income=300_000_000_000, eps=3000, total_equity=1_000_000_000_000,
            total_assets=3_000_000_000_000, dividend_per_share=1500,
            operating_income=400_000_000_000,
        ))
        fin_repo.replace_period(conn, filing_id=filing_id, financials=fin_repo.FinancialsRow(
            ticker="SNTS", period_year=2023, revenue=1_200_000_000_000,
            net_income=240_000_000_000, eps=2400, total_equity=900_000_000_000,
            total_assets=2_800_000_000_000, dividend_per_share=1200,
            operating_income=320_000_000_000,
        ))

    series = ratios_svc.get_ratios_series("SNTS", limit=5)
    assert [(s.period_year, s.period_kind) for s in series] == [
        (2024, "annual"), (2023, "annual"),
    ]

    # Newest row uses the older row as its prior for growth.
    latest = series[0]
    assert latest.revenue_growth is not None
    assert latest.revenue_growth.value == pytest.approx(25.0)  # (1.5T - 1.2T) / 1.2T
    assert latest.net_income_growth is not None
    assert latest.net_income_growth.value == pytest.approx(25.0)

    # P/E uses shares * price so this is the SONATEL-sized number: 32500 / 3000 ~ 10.83.
    assert latest.pe.value == pytest.approx(32_500.0 / 3000.0)
    assert latest.market_cap_xof == pytest.approx(100_000_000 * 32_500.0)
    assert latest.currency == "XOF"

    # The oldest row has no earlier data → growth ratios are None.
    oldest = series[-1]
    assert oldest.revenue_growth is None


def test_get_latest_ratios_returns_head_of_series(monkeypatch, tmp_path):
    db_path, ratios_svc, fin_repo = _setup(monkeypatch, tmp_path)
    with connect(db_path) as conn:
        from brvm.models import Filing
        from brvm.store import filings as filings_repo
        filings_repo.upsert_filings(conn, [Filing(
            ticker="SNTS", issuer_name="SONATEL", doc_type="rapport_annuel",
            period_kind="annual", period_year=2024, source="brvm_org",
            source_url="u1", url_hash="h1",
            file_path="p1", size_bytes=1, sha256="a", page_count=1,
        )])
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        fin_repo.replace_period(conn, filing_id=filing_id, financials=fin_repo.FinancialsRow(
            ticker="SNTS", period_year=2024, revenue=1_000_000, net_income=200_000,
            eps=100, total_equity=500_000, total_assets=1_000_000,
        ))

    latest = ratios_svc.get_latest_ratios("SNTS")
    assert latest is not None
    assert latest.period_year == 2024


def test_get_latest_ratios_returns_none_when_no_financials(monkeypatch, tmp_path):
    _db, ratios_svc, _fin_repo = _setup(monkeypatch, tmp_path)
    assert ratios_svc.get_latest_ratios("SNTS") is None


def test_get_ratios_for_interim_uses_most_recent_interim(monkeypatch, tmp_path):
    db_path, ratios_svc, fin_repo = _setup(monkeypatch, tmp_path)
    with connect(db_path) as conn:
        from brvm.models import Filing
        from brvm.store import filings as filings_repo
        filings_repo.upsert_filings(conn, [Filing(
            ticker="SNTS", issuer_name="SONATEL", doc_type="rapport_activites",
            period_kind="H1", period_year=2025, source="brvm_org",
            source_url="u1", url_hash="h1",
            file_path="p1", size_bytes=1, sha256="a", page_count=1,
        )])
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        fin_repo.replace_period(conn, filing_id=filing_id, financials=fin_repo.FinancialsRow(
            ticker="SNTS", period_year=2025, period_kind="H1",
            revenue=500_000, net_income=90_000, eps=50, total_equity=400_000,
            total_assets=1_000_000, operating_income=100_000,
        ))

    view = ratios_svc.get_ratios_for_interim("SNTS")
    assert view is not None
    assert view.period_year == 2025 and view.period_kind == "H1"
    assert view.net_margin.value == pytest.approx(18.0)  # 90k / 500k
    # No prior period for interim ratios (growth intentionally not shown).
    assert view.revenue_growth is None
