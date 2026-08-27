"""Bond math + view service.

Everything the `/s/{ticker}/{overview,cashflow,yield,related}` tabs
need is composed here so the Web and TUI templates stay dumb views over
a single view-model. The pricing math (YTM, duration, convexity)
assumes bullet-and-annual, which matches ~all currently-listed BRVM
bonds; amortising and quarterly-pay issues (rare — some FCTC / social
bonds) surface a footnote noting the assumption instead of trying to
derive the schedule from a name we can't trust.

BRVM bond nominal is nearly always 10 000 XOF (a handful of legacy
issues are at 1 000). We default to 10 000 and expose the assumption on
the Cash flow tab so a reader can spot the mismatch — the price column
on the source page is close enough to par for real bonds that the
default is a safe fallback when we're missing an explicit nominal.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from brvm.config import settings
from brvm.db import connect
from brvm.models import BondSnapshot
from brvm.store import bonds as bonds_repo

DEFAULT_NOMINAL_XOF = 10_000.0

# YTM bisection window. -50% (deeply discounted, close to default) …
# +100% (freshly-admitted at a fraction of par) covers every realistic
# case we've seen on brvm.org; the solver returns None outside the
# bracket rather than clamping.
_YTM_LOW = -0.50
_YTM_HIGH = 1.00
_YTM_TOL = 1e-7
_YTM_MAX_ITER = 100


def _db_path() -> Path:
    return Path(settings.db_path)


# ---------- date arithmetic -------------------------------------------------


def _add_years(d: date, n: int) -> date:
    """Shift `d` by `n` years, snapping Feb 29 → Feb 28 in non-leap years.

    Used to walk coupon anniversaries and to derive the maturity date
    from `last_coupon_date` (which the exchange treats as an authoritative
    anchor — it's the actual disbursed anniversary, not a formal
    contractual date derived from the issue-date anniversary).
    """
    year = d.year + n
    day = d.day
    if d.month == 2 and d.day == 29:
        # Non-leap target: snap to Feb 28.
        try:
            return d.replace(year=year)
        except ValueError:
            return date(year, 2, 28)
    return date(year, d.month, day)


# ---------- cash-flow schedule ---------------------------------------------


@dataclass(frozen=True)
class CashFlowRow:
    payment_date: date
    coupon: float
    principal: float
    total: float
    # Years from today to the payment (float, e.g. 0.42 for a coupon
    # 155 days out). Used as the exponent in the YTM discount factor.
    year_fraction: float

    @property
    def is_terminal(self) -> bool:
        return self.principal > 0


@dataclass(frozen=True)
class BondSchedule:
    rows: list[CashFlowRow]
    nominal: float
    annual_coupon: float
    next_coupon_date: date | None
    coupons_remaining: int


def build_schedule(
    *,
    coupon_rate: float,
    maturity_year: int,
    last_coupon_date: date | None,
    issue_date: date | None,
    today: date,
    nominal: float = DEFAULT_NOMINAL_XOF,
) -> BondSchedule | None:
    """Bullet + annual coupon schedule.

    The anchor for coupon anniversaries is `last_coupon_date` when known
    (it's what the exchange actually disbursed most recently — a real
    payment beats a derived one) and falls back to `issue_date` for
    freshly-admitted bonds whose first coupon hasn't paid yet. Without
    either, we can't place the coupon flow on the calendar and return
    None so the tab renders a "schedule unavailable" state instead of
    fabricating dates.
    """
    if coupon_rate is None or maturity_year is None:
        return None
    anchor = last_coupon_date or issue_date
    if anchor is None:
        return None

    annual_coupon = coupon_rate / 100.0 * nominal

    # Walk anniversaries forward from the anchor. The first future
    # anniversary is the next coupon.
    next_dt = anchor
    while next_dt <= today:
        next_dt = _add_years(next_dt, 1)

    # Cap at year-end of `maturity_year` — the exchange doesn't publish
    # the exact maturity day, and the last anniversary that falls within
    # the maturity year is the closest thing to a canonical maturity date.
    def _within_maturity(d: date) -> bool:
        return d.year <= maturity_year

    rows: list[CashFlowRow] = []
    dt = next_dt
    while _within_maturity(dt):
        # Days-based year fraction so the discount factor cares about
        # actual elapsed calendar time, not just an integer count.
        t_years = (dt - today).days / 365.25
        is_terminal = _add_years(dt, 1).year > maturity_year
        principal = nominal if is_terminal else 0.0
        rows.append(
            CashFlowRow(
                payment_date=dt,
                coupon=annual_coupon,
                principal=principal,
                total=annual_coupon + principal,
                year_fraction=round(t_years, 4),
            )
        )
        if is_terminal:
            break
        dt = _add_years(dt, 1)

    return BondSchedule(
        rows=rows,
        nominal=nominal,
        annual_coupon=annual_coupon,
        next_coupon_date=next_dt if rows else None,
        coupons_remaining=len(rows),
    )


# ---------- yield / duration / convexity -----------------------------------


def _pv(rows: list[CashFlowRow], y: float) -> float:
    return sum(r.total / (1.0 + y) ** r.year_fraction for r in rows)


def solve_ytm(rows: list[CashFlowRow], dirty_price: float) -> float | None:
    """Return the yield that discounts `rows` to `dirty_price`, or None
    if the target price sits outside the bisection window (implausibly
    high or low).

    Uses classic bisection (100 iterations, 1e-7 tolerance) because it
    can't get trapped on a coupon-year discontinuity the way a Newton
    solver sometimes does on the first few periods of a deeply-discounted
    bond.
    """
    if not rows or dirty_price <= 0:
        return None
    lo, hi = _YTM_LOW, _YTM_HIGH
    f_lo = _pv(rows, lo) - dirty_price
    f_hi = _pv(rows, hi) - dirty_price
    if f_lo * f_hi > 0:
        return None
    for _ in range(_YTM_MAX_ITER):
        mid = 0.5 * (lo + hi)
        f_mid = _pv(rows, mid) - dirty_price
        if abs(f_mid) < _YTM_TOL or (hi - lo) < _YTM_TOL:
            return mid
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


def macaulay_duration(rows: list[CashFlowRow], y: float, dirty_price: float) -> float | None:
    if dirty_price <= 0:
        return None
    return sum(
        r.year_fraction * r.total / (1.0 + y) ** r.year_fraction for r in rows
    ) / dirty_price


def modified_duration(rows: list[CashFlowRow], y: float, dirty_price: float) -> float | None:
    mac = macaulay_duration(rows, y, dirty_price)
    if mac is None:
        return None
    return mac / (1.0 + y)


def convexity(rows: list[CashFlowRow], y: float, dirty_price: float) -> float | None:
    if dirty_price <= 0:
        return None
    return sum(
        r.year_fraction * (r.year_fraction + 1) * r.total / (1.0 + y) ** (r.year_fraction + 2) for r in rows
    ) / dirty_price


def current_yield(coupon_rate: float | None, price: float | None) -> float | None:
    if coupon_rate is None or price is None or price <= 0:
        return None
    return (coupon_rate / 100.0) * DEFAULT_NOMINAL_XOF / price * 100.0


# ---------- read-side view models ------------------------------------------


@dataclass(frozen=True)
class YieldSummary:
    ytm_pct: float | None
    current_yield_pct: float | None
    modified_duration_years: float | None
    macaulay_duration_years: float | None
    convexity: float | None
    dirty_price: float | None
    clean_price: float | None
    accrued_coupon: float | None


@dataclass(frozen=True)
class RelatedBond:
    ticker: str
    name: str
    coupon_rate: float | None
    maturity_year: int | None
    country: str | None
    sector: str | None
    is_matured: bool


@dataclass(frozen=True)
class ProspectusNews:
    id: int
    title: str
    url: str
    published_at: str | None
    kind: str


@dataclass(frozen=True)
class BondView:
    ticker: str
    name: str
    issuer_name: str | None
    sector: str | None
    country: str | None
    source_url: str | None
    coupon_rate: float | None
    maturity_year: int | None
    issue_date: date | None
    clean_price: float | None
    last_snapshot: BondSnapshot | None
    schedule: BondSchedule | None
    yield_: YieldSummary | None
    related: list[RelatedBond] = field(default_factory=list)
    issuer_equity_ticker: str | None = None  # cross-link if the issuer is also listed as equity
    prospectus_news: list[ProspectusNews] = field(default_factory=list)


def _latest_close(conn: sqlite3.Connection, ticker: str) -> float | None:
    row = conn.execute(
        "SELECT close FROM daily_bars WHERE ticker = ? ORDER BY session_date DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return row["close"] if row else None


# Issuer-name tokens that never identify a company (WAEMU sovereigns,
# generic-adjective prefixes). Filtering these out keeps `_find_issuer_equity`
# from cross-linking every state bond to some equity that happens to
# start with "ETAT".
_ISSUER_STOPWORDS: frozenset[str] = frozenset({
    "ETAT", "TRESOR", "PUBLIC", "REPUBLIQUE", "DU", "DE", "DES", "LA", "LE",
})


def _issuer_brand_token(issuer_name: str) -> str | None:
    """Return the first non-stopword token of the issuer name — the
    "brand" (e.g. `ECOBANK` from `ECOBANK CI`, `NSIA` from `NSIA BQE CI`,
    None from `ETAT DU MALI` because the whole thing is stopwords).

    Cross-linking on the brand token catches sibling listings like
    `ECOBANK CI` (bond) → `ECOBANK TRANSNATIONAL INC` (equity) that a
    naive substring match on the full issuer name misses.
    """
    for tok in issuer_name.split():
        clean = tok.strip("-.,").upper()
        if clean and clean not in _ISSUER_STOPWORDS:
            return clean
    return None


def _find_issuer_equity(conn: sqlite3.Connection, issuer_name: str) -> str | None:
    """Return the equity ticker whose `name` contains the issuer's brand
    token, if one exists. Returns None for sovereign issuers whose brand
    is a stopword ("ETAT DU MALI" → no cross-link) so state bonds don't
    accidentally point at random equities.
    """
    if not issuer_name:
        return None
    brand = _issuer_brand_token(issuer_name)
    if brand is None or len(brand) < 3:
        # Very short tokens ("CI", "BF") aren't specific enough to avoid
        # false positives — bail out rather than link to something wrong.
        return None
    row = conn.execute(
        """
        SELECT ticker FROM securities
        WHERE kind = 'equity' AND active = 1
          AND UPPER(name) LIKE ?
        ORDER BY ticker
        LIMIT 1
        """,
        (f"%{brand}%",),
    ).fetchone()
    return row["ticker"] if row else None


def _list_prospectus_news(
    conn: sqlite3.Connection, issuer_name: str, limit: int = 5
) -> list[ProspectusNews]:
    """Match `news_items` where issuer_name equals or the title mentions
    the issuer + 'obligation' / 'cotation' / 'admission' — a low-effort
    Bloomberg-style prospectus link surface. Ordered newest-first."""
    if not issuer_name:
        return []
    like_issuer = f"%{issuer_name}%"
    rows = conn.execute(
        """
        SELECT id, title, url, published_at, kind
        FROM news_items
        WHERE
          (issuer_name = ? OR UPPER(title) LIKE UPPER(?))
          AND (
              LOWER(title) LIKE '%obligat%' OR
              LOWER(title) LIKE '%cotation%' OR
              LOWER(title) LIKE '%admission%'
          )
        ORDER BY COALESCE(published_at, fetched_utc) DESC
        LIMIT ?
        """,
        (issuer_name, like_issuer, limit),
    ).fetchall()
    return [
        ProspectusNews(
            id=r["id"], title=r["title"], url=r["url"],
            published_at=r["published_at"], kind=r["kind"],
        )
        for r in rows
    ]


def _list_related(
    conn: sqlite3.Connection, issuer_name: str, exclude_ticker: str, today: date
) -> list[RelatedBond]:
    rows = bonds_repo.list_by_issuer(conn, issuer_name, exclude_ticker=exclude_ticker)
    out: list[RelatedBond] = []
    for r in rows:
        my = r["maturity_year"]
        out.append(
            RelatedBond(
                ticker=r["ticker"],
                name=r["name"],
                coupon_rate=r["coupon_rate"],
                maturity_year=my,
                country=r["country"],
                sector=r["sector"],
                is_matured=(my is not None and my < today.year),
            )
        )
    return out


def _fmt_bond_view(row: sqlite3.Row, snap: BondSnapshot | None, price: float | None,
                   schedule: BondSchedule | None, ysum: YieldSummary | None,
                   related: list[RelatedBond], equity_link: str | None,
                   prospectus: list[ProspectusNews]) -> BondView:
    issue_date_raw = row["issue_date"]
    issue_date_val = date.fromisoformat(issue_date_raw) if issue_date_raw else None
    return BondView(
        ticker=row["ticker"],
        name=row["name"],
        issuer_name=row["issuer_name"],
        sector=row["sector"],
        country=row["country"],
        source_url=row["source_url"],
        coupon_rate=row["coupon_rate"],
        maturity_year=row["maturity_year"],
        issue_date=issue_date_val,
        clean_price=price,
        last_snapshot=snap,
        schedule=schedule,
        yield_=ysum,
        related=related,
        issuer_equity_ticker=equity_link,
        prospectus_news=prospectus,
    )


def get_bond_view(ticker: str, today: date | None = None) -> BondView | None:
    """Compose the full bond-tab view model for `ticker`.

    Returns None when the ticker isn't a bond so the caller can 404
    cleanly. Every derived field (schedule, yield summary, related list)
    degrades to None / empty when the underlying inputs are missing —
    e.g. a bond without `last_coupon_date` renders "schedule unavailable"
    but still shows the reference block.
    """
    ticker = ticker.upper()
    today = today or date.today()
    with connect(_db_path()) as conn:
        row = conn.execute(
            """
            SELECT ticker, name, kind, country, sector, source_url,
                   coupon_rate, maturity_year, issue_date, issuer_name
            FROM securities WHERE ticker = ?
            """,
            (ticker,),
        ).fetchone()
        if row is None or row["kind"] != "bond":
            return None

        snap = bonds_repo.latest_snapshot(conn, ticker)
        price = _latest_close(conn, ticker)

        issue_date_val = (
            date.fromisoformat(row["issue_date"]) if row["issue_date"] else None
        )
        last_coupon = snap.last_coupon_date if snap else None
        schedule = build_schedule(
            coupon_rate=row["coupon_rate"],
            maturity_year=row["maturity_year"],
            last_coupon_date=last_coupon,
            issue_date=issue_date_val,
            today=today,
        )

        ysum = None
        if schedule and price is not None:
            accrued = snap.accrued_coupon if snap else 0.0
            accrued_val = accrued if accrued is not None else 0.0
            dirty = price + accrued_val
            y = solve_ytm(schedule.rows, dirty)
            mac = macaulay_duration(schedule.rows, y, dirty) if y is not None else None
            mod = modified_duration(schedule.rows, y, dirty) if y is not None else None
            conv = convexity(schedule.rows, y, dirty) if y is not None else None
            ysum = YieldSummary(
                ytm_pct=y * 100.0 if y is not None else None,
                current_yield_pct=current_yield(row["coupon_rate"], price),
                modified_duration_years=mod,
                macaulay_duration_years=mac,
                convexity=conv,
                dirty_price=dirty,
                clean_price=price,
                accrued_coupon=accrued_val,
            )
        elif price is not None:
            ysum = YieldSummary(
                ytm_pct=None,
                current_yield_pct=current_yield(row["coupon_rate"], price),
                modified_duration_years=None,
                macaulay_duration_years=None,
                convexity=None,
                dirty_price=None,
                clean_price=price,
                accrued_coupon=snap.accrued_coupon if snap else None,
            )

        related = (
            _list_related(conn, row["issuer_name"], ticker, today)
            if row["issuer_name"] else []
        )
        equity_link = (
            _find_issuer_equity(conn, row["issuer_name"])
            if row["issuer_name"] else None
        )
        prospectus = (
            _list_prospectus_news(conn, row["issuer_name"])
            if row["issuer_name"] else []
        )

    return _fmt_bond_view(row, snap, price, schedule, ysum, related, equity_link, prospectus)


def list_issuer_news(ticker: str, limit: int = 25) -> list[sqlite3.Row]:
    """News rows tagged for this bond via the issuer-name substring
    fallback. The news tagger only stamps equity tickers, so bond News
    tabs won't be populated by the LLM path — this bridging query
    matches on `issuer_name` (populated on communiqués) or title
    substring so the reader still sees issuer coverage.

    Returns raw rows so the caller can reuse the existing `_row_to_news`
    formatter in `services.news`.
    """
    ticker = ticker.upper()
    with connect(_db_path()) as conn:
        row = conn.execute(
            "SELECT issuer_name FROM securities WHERE ticker = ? AND kind = 'bond'",
            (ticker,),
        ).fetchone()
        if row is None or not row["issuer_name"]:
            return []
        issuer = row["issuer_name"]
        rows = conn.execute(
            """
            SELECT * FROM news_items
            WHERE issuer_name = ? OR UPPER(title) LIKE UPPER(?)
            ORDER BY COALESCE(published_at, fetched_utc) DESC
            LIMIT ?
            """,
            (issuer, f"%{issuer}%", limit),
        ).fetchall()
    return list(rows)
