"""Financial ratios computed from `financials` + live quote + company facts.

All ratios are pure functions of their inputs — the only side effect is a
`Ratio` dataclass carrying the computed value plus a short provenance
string so the UI (and the Phase 6 analyst prompt) can show *how* each
number was arrived at.

Design notes
------------
* **No cache, no store.** Ratios are cheap to compute; caching would add
  invalidation surface (price ticks daily, financials on extraction).
* **Missing / zero divisors → None.** Never `inf`, never `NaN`. Tests
  pin this — a `net_income=0` div/share ratio has to render as "—", not
  crash the tab.
* **Growth ratios need the immediately-prior period of the SAME kind.**
  A Q1 → annual comparison would mix period-to-date with full-year and
  is worse than no signal. `growth_ratios` returns None in that case.
* **Currency awareness.** Price and EPS may be in different currencies
  when an issuer reports EUR/USD comparatives — we refuse to compute P/E
  in that case and flag `currency_mismatch=True` on the view so the
  template can render a badge instead of a bogus multiple.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from kodji.config import settings
from kodji.db import connect
from kodji.store import financials as financials_repo
from kodji.store import securities as sec_repo

# --------------------------------------------------------------------------
# Pure inputs / outputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FinancialsInput:
    """The subset of a `financials` row the ratio engine actually needs.

    Kept as a plain dataclass (not a sqlite3.Row) so the pure functions
    below can be tested without a DB and reused by the peers path where
    we assemble inputs from multiple sources."""

    ticker: str
    period_year: int
    period_kind: str = "annual"
    currency: str = "XOF"
    revenue: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    total_assets: float | None = None
    total_equity: float | None = None
    eps: float | None = None
    dividend_per_share: float | None = None
    cash_flow_ops: float | None = None
    capex: float | None = None
    free_cash_flow: float | None = None


@dataclass(frozen=True)
class MarketInput:
    """Live-market side of the ratios. `price` and `price_currency` are
    what the ratio engine treats as the numerator for P/E, P/B, P/S; a
    mismatch against the `FinancialsInput.currency` flips `usable=False`
    on the view and the multi-currency ratios come back as None."""

    price: float | None = None
    price_currency: str = "XOF"
    shares_outstanding: int | None = None
    market_cap_xof: float | None = None    # sikafinance's own snapshot, for cross-check


@dataclass(frozen=True)
class Ratio:
    """One computed ratio plus a short human-readable provenance string.
    Used by the template popover and the Phase 6 analyst prompt."""

    value: float | None
    provenance: str
    # 'x' for multiples (P/E), 'pct' for margins/yields/growth,
    # 'currency' for absolute XOF amounts (market cap).
    unit: str = "x"


@dataclass
class RatiosView:
    """Ratio bundle for one (ticker, period) — the shape the Financials
    tab and Peers page consume."""

    ticker: str
    period_year: int | None = None
    period_kind: str = "annual"
    currency: str = "XOF"
    price: float | None = None
    market_cap_xof: float | None = None       # computed: shares * price
    currency_mismatch: bool = False           # price and financials disagree

    # Valuation
    pe: Ratio | None = None
    pb: Ratio | None = None
    ps: Ratio | None = None
    pfcf: Ratio | None = None
    fcf_yield: Ratio | None = None
    ev_ebitda: Ratio | None = None
    dividend_yield: Ratio | None = None
    payout_ratio: Ratio | None = None
    earnings_yield: Ratio | None = None

    # Profitability
    roe: Ratio | None = None
    roa: Ratio | None = None
    net_margin: Ratio | None = None
    operating_margin: Ratio | None = None

    # Growth (annual-to-annual only; None when we don't have a prior)
    revenue_growth: Ratio | None = None
    net_income_growth: Ratio | None = None
    eps_growth: Ratio | None = None

    # Leverage
    financial_leverage: Ratio | None = None   # total_assets / total_equity
    equity_ratio: Ratio | None = None         # total_equity / total_assets

    @property
    def has_any(self) -> bool:
        for attr in (
            "pe", "pb", "ps", "pfcf", "fcf_yield", "ev_ebitda",
            "dividend_yield", "payout_ratio", "earnings_yield",
            "roe", "roa", "net_margin", "operating_margin",
            "revenue_growth", "net_income_growth", "eps_growth",
            "financial_leverage", "equity_ratio",
        ):
            if getattr(self, attr) is not None:
                return True
        return False


# --------------------------------------------------------------------------
# Ratio primitives — each returns None for missing / zero divisor.
# --------------------------------------------------------------------------


def _safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None:
        return None
    if den == 0:
        return None
    return num / den


def _pct(num: float | None, den: float | None) -> float | None:
    r = _safe_div(num, den)
    return None if r is None else r * 100.0


def valuation_ratios(
    fin: FinancialsInput, mkt: MarketInput, *, currency_mismatch: bool
) -> dict[str, Ratio | None]:
    """P/E, P/B, P/S, dividend yield, payout, earnings yield. Price-based
    ratios are set to None when we lack a price or when the financials'
    currency disagrees with the price currency."""
    price = mkt.price
    shares = mkt.shares_outstanding
    market_cap = _safe_div(shares, 1) and price and shares and price * shares
    market_cap = market_cap or None

    book_value_per_share = _safe_div(fin.total_equity, shares)
    revenue_per_share = _safe_div(fin.revenue, shares)

    def _price_ratio(field: str, denom: float | None) -> Ratio | None:
        if currency_mismatch:
            return None
        # F-28: a negative denominator (loss-making EPS, negative
        # book value from a distressed issuer) yields a negative
        # multiple that reads as "cheap" to a skim reader — the
        # tooltip and the P/FCF path already suppress this shape.
        # Suppress here too for consistency across the whole tab.
        if denom is None or denom <= 0:
            return None
        v = _safe_div(price, denom)
        if v is None:
            return None
        return Ratio(value=v, provenance=f"price / {field}", unit="x")

    # Cash-flow multiples use market cap as the numerator (share-count
    # x price), which cancels the "same-currency-for-EPS" mismatch
    # concern because FCF is denominated in the *financials* currency
    # and market_cap is in the *price* currency. We therefore reuse the
    # `currency_mismatch` gate that P/E / P/S already respect.
    pfcf: Ratio | None = None
    fcf_yield: Ratio | None = None
    ev_ebitda: Ratio | None = None
    if not currency_mismatch and market_cap is not None:
        if fin.free_cash_flow is not None and fin.free_cash_flow > 0:
            pfcf = Ratio(
                value=market_cap / fin.free_cash_flow,
                provenance="market_cap / free_cash_flow",
                unit="x",
            )
            fcf_yield = Ratio(
                value=fin.free_cash_flow / market_cap * 100.0,
                provenance="free_cash_flow / market_cap",
                unit="pct",
            )
        elif fin.free_cash_flow is not None and fin.free_cash_flow <= 0:
            # Negative FCF: report the yield (informative — reads as a
            # negative %) but suppress P/FCF (a negative multiple is a
            # trap for a reader skimming the table).
            fcf_yield = Ratio(
                value=fin.free_cash_flow / market_cap * 100.0,
                provenance="free_cash_flow / market_cap (negative)",
                unit="pct",
            )
        # EV/EBITDA proxy — until we ingest net debt + D&A, EV=market_cap
        # and EBITDA≈operating_income (RBE). Provenance makes the proxy
        # explicit so a reader isn't misled into thinking this is the
        # textbook multiple.
        if fin.operating_income is not None and fin.operating_income > 0:
            ev_ebitda = Ratio(
                value=market_cap / fin.operating_income,
                provenance="market_cap / operating_income (EV proxy, no net debt / D&A yet)",
                unit="x",
            )

    return {
        "pe": _price_ratio("EPS", fin.eps),
        "pb": _price_ratio("book_value_per_share", book_value_per_share),
        "ps": _price_ratio("revenue_per_share", revenue_per_share),
        "pfcf": pfcf,
        "fcf_yield": fcf_yield,
        "ev_ebitda": ev_ebitda,
        "dividend_yield": (
            None if currency_mismatch else (
                Ratio(
                    value=_pct(fin.dividend_per_share, price),
                    provenance="dividend_per_share / price",
                    unit="pct",
                )
                if _pct(fin.dividend_per_share, price) is not None
                else None
            )
        ),
        "payout_ratio": (
            Ratio(
                value=_pct(fin.dividend_per_share, fin.eps),
                provenance="dividend_per_share / EPS",
                unit="pct",
            )
            if _pct(fin.dividend_per_share, fin.eps) is not None
            else None
        ),
        "earnings_yield": (
            None if currency_mismatch else (
                Ratio(
                    value=_pct(fin.eps, price),
                    provenance="EPS / price",
                    unit="pct",
                )
                if _pct(fin.eps, price) is not None
                else None
            )
        ),
    }


def profitability_ratios(fin: FinancialsInput) -> dict[str, Ratio | None]:
    """ROE, ROA, net margin, operating margin. All pure-financials so no
    currency mismatch concerns."""

    def _pctr(num: float | None, den: float | None, prov: str) -> Ratio | None:
        v = _pct(num, den)
        return Ratio(value=v, provenance=prov, unit="pct") if v is not None else None

    return {
        "roe":              _pctr(fin.net_income, fin.total_equity, "net_income / total_equity"),
        "roa":              _pctr(fin.net_income, fin.total_assets, "net_income / total_assets"),
        "net_margin":       _pctr(fin.net_income, fin.revenue,      "net_income / revenue"),
        "operating_margin": _pctr(fin.operating_income, fin.revenue, "operating_income / revenue"),
    }


def growth_ratios(
    curr: FinancialsInput, prior: FinancialsInput | None
) -> dict[str, Ratio | None]:
    """YoY revenue / net income / EPS growth. Requires a prior period of
    the *same* kind (annual↔annual, H1↔H1) — otherwise returns None so a
    Q1 doesn't get compared to a full-year and produce a meaningless
    multiple."""
    if prior is None:
        return {"revenue_growth": None, "net_income_growth": None, "eps_growth": None}
    if prior.period_kind != curr.period_kind:
        return {"revenue_growth": None, "net_income_growth": None, "eps_growth": None}
    # F-28: growth across a gap year (2024 vs. 2022 because 2023's
    # filing never landed) is not "YoY" — the label the template
    # renders — and mixing it in with true YoY rows misleads a
    # skim reader. Suppress rather than mis-label.
    if abs(curr.period_year - prior.period_year) > 1:
        return {"revenue_growth": None, "net_income_growth": None, "eps_growth": None}

    def _yoy(now: float | None, then: float | None, label: str) -> Ratio | None:
        if now is None or then is None or then == 0:
            return None
        # F-08: `(now - then) / then` flips sign when `then` is negative
        # (loss worsening from -100 to -200 rendered "+100% growth";
        # recovery from -100 to +50 rendered "-150%"). Divide by |then|
        # so the direction matches the intuitive sign: numerator alone
        # already carries the "improved vs. deteriorated" signal.
        v = (now - then) / abs(then) * 100.0
        return Ratio(
            value=v,
            provenance=f"{label}({curr.period_year}) vs {label}({prior.period_year})",
            unit="pct",
        )

    return {
        "revenue_growth":    _yoy(curr.revenue,    prior.revenue,    "revenue"),
        "net_income_growth": _yoy(curr.net_income, prior.net_income, "net_income"),
        "eps_growth":        _yoy(curr.eps,        prior.eps,        "EPS"),
    }


def leverage_ratios(fin: FinancialsInput) -> dict[str, Ratio | None]:
    lev = _safe_div(fin.total_assets, fin.total_equity)
    eq_ratio = _pct(fin.total_equity, fin.total_assets)
    return {
        "financial_leverage": (
            Ratio(value=lev, provenance="total_assets / total_equity", unit="x")
            if lev is not None else None
        ),
        "equity_ratio": (
            Ratio(value=eq_ratio, provenance="total_equity / total_assets", unit="pct")
            if eq_ratio is not None else None
        ),
    }


def compute_ratios(
    fin: FinancialsInput,
    mkt: MarketInput,
    *,
    prior: FinancialsInput | None = None,
) -> RatiosView:
    """Assemble the full ratio bundle for one period."""
    currency_mismatch = bool(
        mkt.price is not None
        and mkt.price_currency
        and fin.currency
        and mkt.price_currency != fin.currency
    )

    market_cap: float | None = None
    if mkt.price is not None and mkt.shares_outstanding:
        market_cap = mkt.price * mkt.shares_outstanding

    view = RatiosView(
        ticker=fin.ticker,
        period_year=fin.period_year,
        period_kind=fin.period_kind,
        currency=fin.currency,
        price=mkt.price,
        market_cap_xof=market_cap,
        currency_mismatch=currency_mismatch,
    )
    for attr, ratio in valuation_ratios(fin, mkt, currency_mismatch=currency_mismatch).items():
        setattr(view, attr, ratio)
    for attr, ratio in profitability_ratios(fin).items():
        setattr(view, attr, ratio)
    for attr, ratio in growth_ratios(fin, prior).items():
        setattr(view, attr, ratio)
    for attr, ratio in leverage_ratios(fin).items():
        setattr(view, attr, ratio)
    return view


# --------------------------------------------------------------------------
# DB-facing helpers — the UI layer only ever calls these two.
# --------------------------------------------------------------------------


def _row_to_input(row: sqlite3.Row) -> FinancialsInput:
    keys = row.keys()
    return FinancialsInput(
        ticker=row["ticker"],
        period_year=int(row["period_year"]),
        period_kind=row["period_kind"],
        currency=row["currency"] or "XOF",
        revenue=row["revenue"],
        operating_income=row["operating_income"],
        net_income=row["net_income"],
        total_assets=row["total_assets"],
        total_equity=row["total_equity"],
        eps=row["eps"],
        dividend_per_share=row["dividend_per_share"],
        # `_row_to_input` is called from two paths — the annual `SELECT *`
        # and the interim projection query. Fall back to None so a
        # projection that omits these columns doesn't KeyError.
        cash_flow_ops=row["cash_flow_ops"] if "cash_flow_ops" in keys else None,
        capex=row["capex"] if "capex" in keys else None,
        free_cash_flow=row["free_cash_flow"] if "free_cash_flow" in keys else None,
    )


def _latest_price(conn: sqlite3.Connection, ticker: str) -> float | None:
    row = conn.execute(
        """
        SELECT last FROM quote_snapshots
        WHERE ticker = ? AND last IS NOT NULL
        ORDER BY captured_utc DESC
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    return row["last"] if row else None


def _market_input(conn: sqlite3.Connection, ticker: str) -> MarketInput:
    facts = sec_repo.get_company_facts(conn, ticker)
    return MarketInput(
        price=_latest_price(conn, ticker),
        price_currency="XOF",   # BRVM equities settle in XOF; only issuer reports vary
        shares_outstanding=facts["shares_outstanding"] if facts else None,
        market_cap_xof=facts["market_cap_xof"] if facts else None,
    )


def get_ratios_series(
    ticker: str, *, period_kind: str = "annual", limit: int = 6
) -> list[RatiosView]:
    """Ratio bundles for the last N annual periods, most recent first.

    Growth ratios walk the returned list in order, so the oldest period
    always has growth = None (nothing prior to compare against)."""
    with connect(settings.db_path) as conn:
        rows = financials_repo.list_financials(
            conn, ticker, period_kind=period_kind, limit=limit
        )
        mkt = _market_input(conn, ticker)

    if not rows:
        return []
    inputs = [_row_to_input(r) for r in rows]

    out: list[RatiosView] = []
    for i, fin in enumerate(inputs):
        # `inputs` is newest-first, so the "prior" period is the next
        # index (i+1). The oldest row's prior is None.
        prior = inputs[i + 1] if i + 1 < len(inputs) else None
        out.append(compute_ratios(fin, mkt, prior=prior))
    return out


def get_latest_ratios(ticker: str) -> RatiosView | None:
    """The head of `get_ratios_series` for callers (Peers, analyst prompt)
    that only need one row per ticker."""
    series = get_ratios_series(ticker, limit=2)
    return series[0] if series else None


def get_ratios_for_interim(ticker: str) -> RatiosView | None:
    """Ratios for the latest interim (H1/Q1/Q3) row.

    The interim card on the Financials tab only shows a subset (net
    margin, operating margin, ROE) — the price-based ratios are still
    computed but the template chooses what to render."""
    with connect(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT ticker, period_year, period_kind, currency, revenue,
                   operating_income, net_income, total_assets, total_equity,
                   eps, dividend_per_share, cash_flow_ops, capex,
                   free_cash_flow
            FROM financials
            WHERE ticker = ? AND period_kind IN ('H1', 'Q1', 'Q3', 'other')
            ORDER BY period_year DESC,
                     CASE period_kind
                        WHEN 'Q3' THEN 4
                        WHEN 'H1' THEN 3
                        WHEN 'Q1' THEN 2
                        ELSE 1 END DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if rows is None:
            return None
        mkt = _market_input(conn, ticker)
    return compute_ratios(_row_to_input(rows), mkt)


# Public API — the templates and Peers importer touch only these.
__all__ = [
    "FinancialsInput",
    "MarketInput",
    "Ratio",
    "RatiosView",
    "compute_ratios",
    "get_latest_ratios",
    "get_ratios_for_interim",
    "get_ratios_series",
    "growth_ratios",
    "leverage_ratios",
    "profitability_ratios",
    "valuation_ratios",
]


def _resolve_db_path() -> Path:
    """Convenience for tests that want to point the module at a fresh DB
    without a full config reload — currently unused inside this module but
    kept so external test helpers can locate the same settings snapshot."""
    return Path(settings.db_path)
