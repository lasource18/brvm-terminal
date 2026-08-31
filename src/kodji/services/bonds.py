"""Bond math + view service.

Everything the `/s/{ticker}/{overview,cashflow,yield,related}` tabs
need is composed here so the Web and TUI templates stay dumb views over
a single view-model. The schedule builder walks bullet-redemption
timelines at the inferred coupon cadence (annual, semi-annual, or
quarterly); the exchange doesn't publish frequency structurally so we
infer it from `last_coupon_amount / (rate/100 x price)` — see
`infer_bond_terms`. Amortising issues fall out of the same inference
because a bond quoted at ~residual with an "annual-shape" coupon lands
as `(residual = price, payments_per_year = 1)`.

BRVM bond nominal is nearly always 10 000 XOF (a handful of legacy
issues are at 1 000). We default to 10 000 when we lack the inputs to
infer otherwise, and expose the inferred residual + cadence on the tab
so a reader can spot a mismatch.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from kodji.config import settings
from kodji.db import connect
from kodji.models import BondSnapshot
from kodji.store import bonds as bonds_repo

DEFAULT_NOMINAL_XOF = 10_000.0

# Recognised coupon cadences. Bonds outside this set (monthly, custom
# amortisation schedules) fall back to the default annual assumption
# rather than pretend we can infer them.
_VALID_PPY: frozenset[int] = frozenset({1, 2, 4})

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


def _add_months(d: date, n: int) -> date:
    """Shift `d` by `n` calendar months, clamping day to the target
    month's length (e.g. Aug 31 + 1 month → Sep 30). Used to step
    semi-annual (n=6) and quarterly (n=3) coupon periods."""
    total = d.month - 1 + n
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


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
    payments_per_year: int = 1


def build_schedule(
    *,
    coupon_rate: float,
    maturity_year: int,
    last_coupon_date: date | None,
    issue_date: date | None,
    today: date,
    nominal: float = DEFAULT_NOMINAL_XOF,
    payments_per_year: int = 1,
) -> BondSchedule | None:
    """Bullet-redemption cash-flow schedule at the given coupon cadence.

    The anchor for coupon dates is `last_coupon_date` when known (it's
    what the exchange actually disbursed most recently — a real payment
    beats a derived one) and falls back to `issue_date` for freshly-
    admitted bonds whose first coupon hasn't paid yet. Without either,
    we can't place the coupon flow on the calendar and return None so
    the tab renders a "schedule unavailable" state instead of fabricating
    dates.

    `payments_per_year` is the inferred coupon cadence (1 = annual,
    2 = semi-annual, 4 = quarterly). The period coupon amount is
    `coupon_rate/100 x nominal / payments_per_year`; forward steps are
    `12 / payments_per_year` calendar months from the anchor. Invalid
    cadences silently fall back to annual — the caller supplies an
    inferred value; garbage in shouldn't crash the schedule.
    """
    if coupon_rate is None or maturity_year is None:
        return None
    anchor = last_coupon_date or issue_date
    if anchor is None:
        return None

    if payments_per_year not in _VALID_PPY:
        payments_per_year = 1
    step_months = 12 // payments_per_year
    annual_coupon = coupon_rate / 100.0 * nominal
    period_coupon = annual_coupon / payments_per_year

    # Walk anniversaries forward from the anchor. The first future
    # date at the coupon cadence is the next payment.
    next_dt = anchor
    while next_dt <= today:
        next_dt = _add_months(next_dt, step_months)

    # Cap at year-end of `maturity_year` — the exchange doesn't publish
    # the exact maturity day, so we treat the last coupon in the maturity
    # year as terminal.
    def _within_maturity(d: date) -> bool:
        return d.year <= maturity_year

    rows: list[CashFlowRow] = []
    dt = next_dt
    while _within_maturity(dt):
        # Days-based year fraction so the discount factor cares about
        # actual elapsed calendar time, not just an integer count.
        t_years = (dt - today).days / 365.25
        is_terminal = not _within_maturity(_add_months(dt, step_months))
        principal = nominal if is_terminal else 0.0
        rows.append(
            CashFlowRow(
                payment_date=dt,
                coupon=period_coupon,
                principal=principal,
                total=period_coupon + principal,
                year_fraction=round(t_years, 4),
            )
        )
        if is_terminal:
            break
        dt = _add_months(dt, step_months)

    # F-31: a bond whose last coupon anniversary is now past the maturity
    # year produces an empty forward schedule. Return None so the tab
    # renders "schedule unavailable" instead of a header-only cash-flow
    # table that silently drops the terminal principal row.
    if not rows:
        return None

    return BondSchedule(
        rows=rows,
        nominal=nominal,
        annual_coupon=annual_coupon,
        next_coupon_date=next_dt,
        coupons_remaining=len(rows),
        payments_per_year=payments_per_year,
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


def current_yield(
    coupon_rate: float | None,
    price: float | None,
    residual_nominal: float | None = None,
) -> float | None:
    """Annual coupon in XOF divided by market price, in percent.

    `residual_nominal` (F-09) is what the coupon actually accrues on
    today. BRVM issues are frequently amortizing and quoted at the
    residual balance (BIDC.O4: 6.10%, 2017-2027, quoted 1 250 XOF
    against a 1 250 residual after seven years of 12.5%/yr amortization).
    Passing the residual gives `coupon / price` at par ≈ the coupon
    rate — the correct current yield. Without it we default to the
    10 000 XOF issuance nominal and the same fixture row renders
    "48.80% current yield" alongside a "—" YTM, which flags a solvent
    supranational as distressed.
    """
    if coupon_rate is None or price is None or price <= 0:
        return None
    nominal = residual_nominal if residual_nominal else DEFAULT_NOMINAL_XOF
    return (coupon_rate / 100.0) * nominal / price * 100.0


def derive_residual_nominal(
    snap: BondSnapshot | None, coupon_rate: float | None
) -> float | None:
    """Recover a bond's residual face value from the exchange-published
    last-coupon amount, assuming ANNUAL coupon cadence:
    `residual = last_coupon_amount / (coupon_rate / 100)`.

    Returns None when either input is missing or the coupon rate is 0
    (avoids a divide-by-zero on a placeholder row). Prefer
    `infer_bond_terms` for the full picture — this helper stays for
    callers that only need the annual-shape derivation and don't have
    a price to disambiguate semi-annual from amortising.
    """
    if snap is None or coupon_rate is None or coupon_rate == 0:
        return None
    amount = getattr(snap, "last_coupon_amount", None)
    if amount is None:
        return None
    return amount * 100.0 / coupon_rate


@dataclass(frozen=True)
class BondTerms:
    """Inferred structural facts about a bond used by the schedule and
    yield math. Everything is a best-effort inference from the exchange's
    published price + last-coupon-amount; we prefer being wrong-by-annual
    (the pre-inference default) over being wrong-by-guessed-cadence when
    the inputs don't clearly point at a semi-annual or quarterly shape.
    """

    residual_nominal: float | None
    payments_per_year: int  # 1, 2, or 4
    confidence: str  # "high" | "low"


def infer_bond_terms(
    snap: BondSnapshot | None,
    coupon_rate: float | None,
    price: float | None,
) -> BondTerms:
    """Infer (residual, coupon cadence) from the exchange snapshot + price.

    BRVM doesn't publish coupon frequency structurally, so we lean on
    the ratio `price / (last_coupon_amount x 100 / coupon_rate)`. The
    denominator is what `derive_residual_nominal` computes — a residual
    that assumes annual cadence. Under real annual cadence the ratio
    trends to 1.0 (price sits near residual par); under semi-annual, the
    denominator is half the true residual, so the ratio trends to 2.0;
    quarterly trends to 4.0. Amortising annual issues (BIDC.O4-shape)
    also land at ratio ≈ 1.0 because both numerator and denominator
    shrink together as the residual pays down.

    Returns `BondTerms(residual=None, payments_per_year=1, "low")` when
    we can't infer — either input missing, coupon rate zero, or a ratio
    that lands between the tolerance bands. Callers fall back to
    `DEFAULT_NOMINAL_XOF` + annual in that case.
    """
    annual_shape_residual = derive_residual_nominal(snap, coupon_rate)
    if annual_shape_residual is None:
        return BondTerms(residual_nominal=None, payments_per_year=1, confidence="low")

    # Without a price we can't disambiguate — return the annual-shape
    # residual and let the caller decide whether it matches reality.
    if price is None or price <= 0:
        return BondTerms(
            residual_nominal=annual_shape_residual,
            payments_per_year=1, confidence="low",
        )

    ratio = price / annual_shape_residual

    # Tolerance bands sized so the ambiguous middle (1.4-1.6, 2.7-3.3)
    # falls through to annual + low confidence rather than mis-classifying
    # a stressed price as a different cadence. Real-data samples land
    # squarely inside these bands.
    if abs(ratio - 1.0) <= 0.35:
        return BondTerms(
            residual_nominal=annual_shape_residual,
            payments_per_year=1, confidence="high",
        )
    if abs(ratio - 2.0) <= 0.30:
        return BondTerms(
            residual_nominal=annual_shape_residual * 2.0,
            payments_per_year=2, confidence="high",
        )
    if abs(ratio - 4.0) <= 0.50:
        return BondTerms(
            residual_nominal=annual_shape_residual * 4.0,
            payments_per_year=4, confidence="high",
        )
    # Ambiguous — fall back to annual on the derived residual, flagged
    # low-confidence so the tab can dim the assumption.
    return BondTerms(
        residual_nominal=annual_shape_residual,
        payments_per_year=1, confidence="low",
    )


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
    prospectus_url: str | None = None  # canonical prospectus / admission link (0016 seed or manual)


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

# F-32: brand-to-equity-name expansions for cases where the bond's
# issuer_name uses a short abbreviation that never appears verbatim in
# the equity's `name`. The equity side has `BANK OF AFRICA BENIN` /
# `BANK OF AFRICA BURKINA FASO` etc.; the bond side has `BOA BENIN`.
# Both point at the same company but a `%BOA%` substring match on the
# equity's name returns zero rows. Each brand maps to the ordered list
# of substrings we should probe against `equities.name` before falling
# back to the raw brand itself.
_BRAND_SYNONYMS: dict[str, tuple[str, ...]] = {
    "BOA": ("BANK OF AFRICA",),
}


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


def _brand_search_patterns(brand: str) -> tuple[str, ...]:
    """Expand a brand token into the ordered LIKE substrings to try
    against `equities.name`. F-32: for brands with a known equity-side
    synonym (`BOA` → `BANK OF AFRICA`), we try the expansion first so
    the cross-link resolves even when the equity's full name never
    carries the short form.
    """
    synonyms = _BRAND_SYNONYMS.get(brand, ())
    # Synonym substrings first (they're more specific), then the brand
    # itself as a fallback for issuers whose full name genuinely
    # contains the short token.
    return (*synonyms, brand)


def _find_issuer_equity(conn: sqlite3.Connection, issuer_name: str) -> str | None:
    """Return the equity ticker whose `name` contains the issuer's brand
    token (or a known synonym), if one exists. Returns None for
    sovereign issuers whose brand is a stopword ("ETAT DU MALI" → no
    cross-link) so state bonds don't accidentally point at random
    equities.
    """
    if not issuer_name:
        return None
    brand = _issuer_brand_token(issuer_name)
    if brand is None or len(brand) < 3:
        # Very short tokens ("CI", "BF") aren't specific enough to avoid
        # false positives — bail out rather than link to something wrong.
        return None
    row = None
    for pattern in _brand_search_patterns(brand):
        row = conn.execute(
            """
            SELECT ticker FROM securities
            WHERE kind = 'equity' AND active = 1
              AND UPPER(name) LIKE ?
            ORDER BY ticker
            LIMIT 1
            """,
            (f"%{pattern}%",),
        ).fetchone()
        if row is not None:
            break
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
                   prospectus: list[ProspectusNews],
                   prospectus_url: str | None) -> BondView:
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
        prospectus_url=prospectus_url,
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
                   coupon_rate, maturity_year, issue_date, issuer_name,
                   prospectus_url
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
        # F-09 + PR-G: infer both the residual balance and the coupon
        # cadence from the exchange's snapshot vs. the market price. The
        # cadence disambiguates semi-annual issues (CRRH.O*, BIDC.O2/O5)
        # from amortising annual issues (BIDC.O4-shape); without it the
        # terminal principal row lands at half the true residual and the
        # YTM comes out biased low.
        terms = infer_bond_terms(snap, row["coupon_rate"], price)
        residual = terms.residual_nominal
        schedule = build_schedule(
            coupon_rate=row["coupon_rate"],
            maturity_year=row["maturity_year"],
            last_coupon_date=last_coupon,
            issue_date=issue_date_val,
            today=today,
            nominal=residual if residual else DEFAULT_NOMINAL_XOF,
            payments_per_year=terms.payments_per_year,
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
                current_yield_pct=current_yield(
                    row["coupon_rate"], price, residual_nominal=residual,
                ),
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
                current_yield_pct=current_yield(
                    row["coupon_rate"], price, residual_nominal=residual,
                ),
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

    return _fmt_bond_view(
        row, snap, price, schedule, ysum, related, equity_link, prospectus,
        row["prospectus_url"],
    )


def pin_prospectus_urls(
    conn: sqlite3.Connection, avis_iter, *, overwrite: bool = False
) -> int:
    """Pin the admission-avis PDF URL on `securities.prospectus_url` for
    every bond ticker referenced by an admission-flagged Avis row.

    `avis_iter` yields `kodji.sources.brvm_org_avis.Avis` dataclasses.
    Non-admission rows are skipped so a coupon-fixing avis doesn't
    overwrite a real admission link. A ticker that isn't in `securities`
    (older re-numbered bond, cross-market listing) is silently ignored.

    Idempotence: by default the UPDATE only fires when `prospectus_url
    IS NULL`, so a manual override made in the DB is respected. Pass
    `overwrite=True` when refreshing after a URL is known to have
    changed (rare — brvm.org's `/sites/default/files/` links are
    stable across years).

    Returns the number of `securities` rows touched. Commits once at
    the end so the whole backfill lands or nothing does.
    """
    from kodji.sources.brvm_org_avis import Avis  # local import: avoids cycle

    pinned = 0
    seen: set[str] = set()
    for a in avis_iter:
        if not isinstance(a, Avis):
            continue
        if not a.is_admission:
            continue
        # Primary path: ticker embedded in title / filename (post-2019
        # avis + all multi-ticker bulk-admission avis).
        candidate_tickers: list[str] = list(a.tickers)
        # Fallback path (issue #49): resolve (issuer, coupon, iy, my)
        # specs against `securities` for older avis whose filenames
        # don't carry the ticker slug (e.g. TPCI 5.85% 2014-2021).
        # Skipped when we already have tickers because the direct
        # extraction is unambiguous and cheaper.
        if not candidate_tickers:
            for spec in a.specs:
                resolved = _resolve_bond_spec(conn, spec)
                if resolved is not None and resolved not in candidate_tickers:
                    candidate_tickers.append(resolved)
        for ticker in candidate_tickers:
            if ticker in seen:
                # Newest wins because callers walk pages newest-first;
                # a later (older) match doesn't get to overwrite.
                continue
            seen.add(ticker)
            if overwrite:
                cur = conn.execute(
                    "UPDATE securities SET prospectus_url = ? "
                    "WHERE ticker = ? AND kind = 'bond'",
                    (a.pdf_url, ticker),
                )
            else:
                cur = conn.execute(
                    "UPDATE securities SET prospectus_url = ? "
                    "WHERE ticker = ? AND kind = 'bond' "
                    "AND prospectus_url IS NULL",
                    (a.pdf_url, ticker),
                )
            pinned += cur.rowcount
    conn.commit()
    return pinned


def _resolve_bond_spec(conn: sqlite3.Connection, spec) -> str | None:
    """Look up the unique bond ticker for a `(issuer_brand, coupon,
    issue_year, maturity_year)` triple.

    Match rule: issuer_name contains the brand token, coupon_rate is
    within 0.005 of the spec (tolerates rounding — the exchange
    quotes 6,55 while some DB rows carry 6.55 vs. 6.5), and
    maturity_year matches exactly. issue_date year matches when
    populated but isn't required — some older rows have NULL
    issue_date.

    Returns None on zero matches (unknown bond, or the audit's
    "matured before we tracked it" case) and on more than one match
    (ambiguous — refuse to guess). The caller logs but doesn't fail
    on either.
    """
    from kodji.sources.brvm_org_avis import BondSpec  # local: avoid cycle
    if not isinstance(spec, BondSpec):
        return None
    like = f"%{spec.issuer_brand}%"
    rows = conn.execute(
        """
        SELECT ticker, issue_date FROM securities
        WHERE kind = 'bond'
          AND UPPER(issuer_name) LIKE ?
          AND coupon_rate IS NOT NULL
          AND ABS(coupon_rate - ?) < 0.005
          AND maturity_year = ?
        """,
        (like, spec.coupon_pct, spec.maturity_year),
    ).fetchall()
    if not rows:
        return None
    # Prefer rows whose stored `issue_date` year matches the avis's
    # issue year — kills ambiguity in the "same issuer re-tapped the
    # same maturity+coupon in a later year" edge case.
    same_year = [
        r for r in rows
        if r["issue_date"]
        and r["issue_date"].startswith(str(spec.issue_year))
    ]
    if len(same_year) == 1:
        return same_year[0]["ticker"]
    if len(rows) == 1:
        return rows[0]["ticker"]
    return None  # ambiguous — refuse to guess


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
