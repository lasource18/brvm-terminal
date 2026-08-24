"""Full securities directory with filters, period returns, and column sort.

The row list feeds `/directory` and its HTMX fragment `/_frag/directory`.
Period returns (1W / 1M / 3M / YTD) are computed against the close-or-
level on-or-before a target reference date so weekends and BRVM holidays
don't produce nulls when a bar just happens to be a day off.

Prices come from a unified CTE that stacks `daily_bars.close` and
`index_levels.level`, so equities and indices share the same reference-
lookup path. The "current price" column still comes from
`quote_snapshots.last` (intraday) so the LAST column moves in real time.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from brvm.clock import session_date_for
from brvm.config import settings
from brvm.db import connect
from brvm.services._view import DirectoryRow


def _db_path() -> Path:
    return Path(settings.db_path)


def distinct_countries() -> list[str]:
    with connect(_db_path()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT country FROM securities "
            "WHERE country IS NOT NULL AND active = 1 ORDER BY country"
        ).fetchall()
    return [r["country"] for r in rows]


def distinct_sectors() -> list[str]:
    with connect(_db_path()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT sector FROM securities "
            "WHERE sector IS NOT NULL AND sector != '' AND active = 1 ORDER BY sector"
        ).fetchall()
    return [r["sector"] for r in rows]


# Whitelisted sort columns. Keys are what the URL accepts; values are the
# expression to sort on inside the enclosing query. Anything else falls
# back to the default multi-key sort. Prevents SQL injection through the
# `sort` query param without a schema-driven mapping.
_SORTABLE: dict[str, str] = {
    "ticker": "s.ticker",
    "name": "s.name",
    "kind": "s.kind",
    "country": "s.country",
    "sector": "s.sector",
    "last": "last",
    "change_pct": "change_pct",
    "change_1w_pct": "change_1w_pct",
    "change_1m_pct": "change_1m_pct",
    "change_3m_pct": "change_3m_pct",
    "change_ytd_pct": "change_ytd_pct",
    "change_1y_pct": "change_1y_pct",
    "change_all_pct": "change_all_pct",
}

# Default order: indices at the top (kind = 'index' > 'equity' > 'bond'
# lexicographically), then group by country, then ticker. Matches the
# pre-4d/e behaviour so bookmarks land in a stable spot.
_DEFAULT_ORDER = "s.kind DESC, s.country IS NULL, s.country, s.ticker"


def _resolve_sort(sort: str | None, direction: str | None) -> tuple[str, str, str]:
    """Return (sort_key, direction, order_by_sql).

    - Unknown sort keys collapse to the default order.
    - Direction is 'asc' or 'desc', defaulting to descending for
      numeric-return columns (users almost always want to see biggest
      movers on top) and ascending for text columns.
    - Numeric columns append `NULLS LAST` so a ticker without a reference
      price doesn't shove real values off the top.
    """
    if sort not in _SORTABLE:
        return "", "", _DEFAULT_ORDER

    numeric = sort not in {"ticker", "name", "kind", "country", "sector"}
    dir_ = (direction or "").lower()
    if dir_ not in ("asc", "desc"):
        dir_ = "desc" if numeric else "asc"

    col = _SORTABLE[sort]
    if numeric:
        # `NULLS LAST` in both directions — a ticker without a reference
        # price shouldn't sit at the top of "biggest gainers" or "biggest
        # losers". Deterministic tiebreak on ticker so equal values
        # (or all-null runs) don't reshuffle between clicks.
        return sort, dir_, f"{col} {dir_.upper()} NULLS LAST, s.ticker ASC"
    return sort, dir_, f"{col} {dir_.upper()}, s.ticker ASC"


def _ref_dates(today: date) -> dict[str, str]:
    """Target reference dates for each period column. `1w`, `1m`, `3m`,
    `1y` are calendar-day windows (SQL picks the closest bar on-or-before).
    YTD is the last trading day of the *prior* year, i.e. the base for
    "year-to-date" is last year's close. All-time is handled separately
    (no target date — just MIN(session_date))."""
    return {
        "1w": (today - timedelta(days=7)).isoformat(),
        "1m": (today - timedelta(days=30)).isoformat(),
        "3m": (today - timedelta(days=90)).isoformat(),
        # `<` today's Jan 1 → last close of the prior year.
        "ytd": date(today.year, 1, 1).isoformat(),
        "1y": (today - timedelta(days=365)).isoformat(),
    }


def list_directory(
    country: str | None = None,
    sector: str | None = None,
    q: str | None = None,
    kind: str | None = None,
    sort: str | None = None,
    direction: str | None = None,
) -> list[DirectoryRow]:
    """Directory rows with period-return columns and optional column sort.

    `sort` accepts any key in `_SORTABLE`; anything else is ignored so a
    stale bookmark can't break the page. `direction` is 'asc' or 'desc'.
    """
    clauses = ["s.active = 1"]
    params: list = []
    if country:
        clauses.append("UPPER(s.country) = ?")
        params.append(country.upper())
    if sector:
        clauses.append("s.sector = ?")
        params.append(sector)
    if kind:
        clauses.append("s.kind = ?")
        params.append(kind)
    if q:
        clauses.append("(UPPER(s.ticker) LIKE ? OR UPPER(s.name) LIKE ?)")
        like = f"%{q.upper()}%"
        params.extend([like, like])
    where = " AND ".join(clauses)

    today = session_date_for()
    refs = _ref_dates(today)
    _sort_key, _dir, order_by = _resolve_sort(sort, direction)

    sql = f"""
    WITH prices AS (
        SELECT ticker, session_date, close AS px FROM daily_bars
        UNION ALL
        SELECT ticker, session_date, level AS px FROM index_levels
    ),
    latest AS (
        SELECT ticker, MAX(session_date) AS d FROM prices GROUP BY ticker
    ),
    ref_1w  AS (
        SELECT p.ticker, p.px FROM prices p
        JOIN (SELECT ticker, MAX(session_date) AS d FROM prices
              WHERE session_date <= ? GROUP BY ticker) x
          ON x.ticker = p.ticker AND x.d = p.session_date
    ),
    ref_1m  AS (
        SELECT p.ticker, p.px FROM prices p
        JOIN (SELECT ticker, MAX(session_date) AS d FROM prices
              WHERE session_date <= ? GROUP BY ticker) x
          ON x.ticker = p.ticker AND x.d = p.session_date
    ),
    ref_3m  AS (
        SELECT p.ticker, p.px FROM prices p
        JOIN (SELECT ticker, MAX(session_date) AS d FROM prices
              WHERE session_date <= ? GROUP BY ticker) x
          ON x.ticker = p.ticker AND x.d = p.session_date
    ),
    -- YTD base = last close of the PRIOR year. `< YYYY-01-01` picks that
    -- naturally; if a ticker has no data before Jan 1 this year (recent
    -- IPO, new index) the LEFT JOIN below leaves change_ytd_pct NULL.
    ref_ytd AS (
        SELECT p.ticker, p.px FROM prices p
        JOIN (SELECT ticker, MAX(session_date) AS d FROM prices
              WHERE session_date < ? GROUP BY ticker) x
          ON x.ticker = p.ticker AND x.d = p.session_date
    ),
    ref_1y  AS (
        SELECT p.ticker, p.px FROM prices p
        JOIN (SELECT ticker, MAX(session_date) AS d FROM prices
              WHERE session_date <= ? GROUP BY ticker) x
          ON x.ticker = p.ticker AND x.d = p.session_date
    ),
    -- All-time base = the earliest recorded price for the ticker. A
    -- brand-new ticker whose earliest bar is also today naturally
    -- returns 0% — the LEFT JOIN can't be null here as long as the
    -- ticker has at least one price row.
    ref_all AS (
        SELECT p.ticker, p.px FROM prices p
        JOIN (SELECT ticker, MIN(session_date) AS d FROM prices
              GROUP BY ticker) x
          ON x.ticker = p.ticker AND x.d = p.session_date
    ),
    latest_q AS (
        SELECT ticker, MAX(captured_utc) AS captured_utc
        FROM quote_snapshots GROUP BY ticker
    ),
    latest_i AS (
        SELECT ticker, MAX(session_date) AS session_date
        FROM index_levels GROUP BY ticker
    )
    SELECT s.ticker, s.name, s.kind, s.country, s.sector,
           COALESCE(qs.last, lp.px)              AS last,
           -- Day change: prefer the intraday snapshot's own change_pct
           -- (equities) and fall back to the latest index_levels row for
           -- indices, which don't populate quote_snapshots.
           COALESCE(qs.change_pct, il.change_pct) AS change_pct,
           CASE WHEN lp.px IS NOT NULL AND r1w.px > 0
                THEN (lp.px - r1w.px) / r1w.px * 100 END AS change_1w_pct,
           CASE WHEN lp.px IS NOT NULL AND r1m.px > 0
                THEN (lp.px - r1m.px) / r1m.px * 100 END AS change_1m_pct,
           CASE WHEN lp.px IS NOT NULL AND r3m.px > 0
                THEN (lp.px - r3m.px) / r3m.px * 100 END AS change_3m_pct,
           CASE WHEN lp.px IS NOT NULL AND rytd.px > 0
                THEN (lp.px - rytd.px) / rytd.px * 100 END AS change_ytd_pct,
           CASE WHEN lp.px IS NOT NULL AND r1y.px > 0
                THEN (lp.px - r1y.px) / r1y.px * 100 END AS change_1y_pct,
           CASE WHEN lp.px IS NOT NULL AND rall.px > 0
                THEN (lp.px - rall.px) / rall.px * 100 END AS change_all_pct
    FROM securities s
    LEFT JOIN latest lat ON lat.ticker = s.ticker
    LEFT JOIN prices lp  ON lp.ticker = lat.ticker AND lp.session_date = lat.d
    LEFT JOIN ref_1w  r1w  ON r1w.ticker  = s.ticker
    LEFT JOIN ref_1m  r1m  ON r1m.ticker  = s.ticker
    LEFT JOIN ref_3m  r3m  ON r3m.ticker  = s.ticker
    LEFT JOIN ref_ytd rytd ON rytd.ticker = s.ticker
    LEFT JOIN ref_1y  r1y  ON r1y.ticker  = s.ticker
    LEFT JOIN ref_all rall ON rall.ticker = s.ticker
    LEFT JOIN latest_q lq ON lq.ticker = s.ticker
    LEFT JOIN quote_snapshots qs
        ON qs.ticker = lq.ticker AND qs.captured_utc = lq.captured_utc
    LEFT JOIN latest_i li ON li.ticker = s.ticker
    LEFT JOIN index_levels il
        ON il.ticker = li.ticker AND il.session_date = li.session_date
    WHERE {where}
    ORDER BY {order_by}
    """

    # `ref_all` has no target date, so it doesn't consume a param — the
    # order here mirrors the CTE order in the SQL exactly.
    ordered_params = [
        refs["1w"], refs["1m"], refs["3m"], refs["ytd"], refs["1y"], *params,
    ]
    with connect(_db_path()) as conn:
        rows = conn.execute(sql, ordered_params).fetchall()
    return [
        DirectoryRow(
            ticker=r["ticker"],
            name=r["name"],
            kind=r["kind"],
            country=r["country"],
            sector=r["sector"],
            last=r["last"],
            change_pct=r["change_pct"],
            change_1w_pct=r["change_1w_pct"],
            change_1m_pct=r["change_1m_pct"],
            change_3m_pct=r["change_3m_pct"],
            change_ytd_pct=r["change_ytd_pct"],
            change_1y_pct=r["change_1y_pct"],
            change_all_pct=r["change_all_pct"],
        )
        for r in rows
    ]
