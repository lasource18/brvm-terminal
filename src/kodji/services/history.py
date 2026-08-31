"""On-demand OHLCV history service with a 15-minute in-memory TTL cache.

Miss path: fetch sikafinance historique, upsert into `daily_bars`, hydrate
the cache. Hit path: return the cached list. If the cache is warm but the
DB has fresher rows (e.g. from a nightly batch we might add later), the
cache is bypassed and rebuilt from the DB.

Intraday overlay
----------------
Sikafinance's `historique` table only publishes today's session **after**
the close (~15:00 Africa/Abidjan). During the trading day, `daily_bars`
therefore stops at yesterday. We synthesise today's in-flight candle
from `quote_snapshots` — the intraday poller writes one row per snapshot
with the sikafinance cotation page's O/H/L/last/volume — and prepend it
to the series when today isn't already in `daily_bars`. Once the nightly
sikafinance fetch overwrites, the synthetic bar drops out naturally.
"""

from __future__ import annotations

import time
from datetime import UTC
from pathlib import Path
from threading import Lock

import httpx

from kodji.clock import session_date_for
from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.models import DailyBar
from kodji.sources import sikafinance
from kodji.sources._http import make_client
from kodji.store import quotes as quotes_repo

_INDEX_SOURCE = "index_levels"
_INTRADAY_SOURCE = "intraday_snapshot"

log = get(__name__)

TTL_S = 15 * 60
_cache: dict[str, tuple[float, list[DailyBar]]] = {}
_lock = Lock()


def _db_path() -> Path:
    return Path(settings.db_path)


def _security_country(ticker: str) -> str | None:
    with connect(_db_path()) as conn:
        r = conn.execute(
            "SELECT country FROM securities WHERE ticker = ?", (ticker,)
        ).fetchone()
    return r["country"] if r else None


def _security_kind(ticker: str) -> str | None:
    with connect(_db_path()) as conn:
        r = conn.execute(
            "SELECT kind FROM securities WHERE ticker = ?", (ticker,)
        ).fetchone()
    return r["kind"] if r else None


def _load_index_levels(ticker: str) -> list[DailyBar]:
    """Load index levels as level-only bars (close = level, no OHLC)."""
    from datetime import date as _date

    with connect(_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT ticker, session_date, level
            FROM index_levels
            WHERE ticker = ?
            ORDER BY session_date DESC
            """,
            (ticker,),
        ).fetchall()
    return [
        DailyBar(
            ticker=r["ticker"],
            session_date=_date.fromisoformat(r["session_date"]),
            close=r["level"],
            source=_INDEX_SOURCE,
        )
        for r in rows
    ]


def _load_from_db(ticker: str) -> list[DailyBar]:
    from datetime import date as _date

    with connect(_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT ticker, session_date, open, high, low, close, volume, turnover, source
            FROM daily_bars
            WHERE ticker = ?
            ORDER BY session_date DESC
            """,
            (ticker,),
        ).fetchall()
    return [
        DailyBar(
            ticker=r["ticker"],
            session_date=_date.fromisoformat(r["session_date"]),
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r["volume"],
            turnover=r["turnover"],
            source=r["source"],
        )
        for r in rows
    ]


def _todays_intraday_bar(ticker: str) -> DailyBar | None:
    """Synthesise today's in-flight bar from `quote_snapshots`.

    Aggregates the O/H/L/close/volume across today's *market-hours*
    captures only:
      * open   — first non-null `open` from today's market-hours snapshots
      * high   — MAX of `high` and `last`
      * low    — MIN of `low` and `last`
      * close  — most recent `last`
      * volume — most recent `volume` (sikafinance publishes cumulative
                 daily volume)

    **Why market-hours only.** Sikafinance's cotation page still shows
    the *previous* session's cumulative data until the market opens.
    The scheduler snapshot job at 00:17 / 01:17 / 03:17 UTC therefore
    captures yesterday's numbers under today's `captured_utc`. Filtering
    to `>= today 09:00 UTC` (Africa/Abidjan opens at 09:00, UTC+0)
    keeps those out of the aggregate. Returns None when there are no
    market-hours captures yet — pre-market renders yesterday's close as
    the last bar rather than fabricating an empty candle.
    """
    from datetime import date as _date

    today = session_date_for()
    day_prefix = today.isoformat()  # 'YYYY-MM-DD' — captured_utc starts with this in Africa/Abidjan (UTC+0)
    market_open_utc = f"{day_prefix}T09:00:00Z"

    with connect(_db_path()) as conn:
        agg = conn.execute(
            """
            SELECT
                MAX(COALESCE(high, last)) AS high,
                MIN(COALESCE(low,  last)) AS low,
                MAX(volume) AS volume
            FROM quote_snapshots
            WHERE ticker = ?
              AND captured_utc >= ?
              AND captured_utc LIKE ?
              AND last IS NOT NULL
            """,
            (ticker, market_open_utc, f"{day_prefix}%"),
        ).fetchone()
        # `open` should be the FIRST captured value of the market session,
        # not the min across snapshots.
        first = conn.execute(
            """
            SELECT "open", last
            FROM quote_snapshots
            WHERE ticker = ?
              AND captured_utc >= ?
              AND captured_utc LIKE ?
              AND last IS NOT NULL
            ORDER BY captured_utc ASC
            LIMIT 1
            """,
            (ticker, market_open_utc, f"{day_prefix}%"),
        ).fetchone()
        latest = conn.execute(
            """
            SELECT last, captured_utc
            FROM quote_snapshots
            WHERE ticker = ?
              AND captured_utc >= ?
              AND captured_utc LIKE ?
              AND last IS NOT NULL
            ORDER BY captured_utc DESC
            LIMIT 1
            """,
            (ticker, market_open_utc, f"{day_prefix}%"),
        ).fetchone()

    if latest is None or agg is None or agg["high"] is None:
        return None

    open_val = (first["open"] if first and first["open"] is not None
                else (first["last"] if first else None))
    return DailyBar(
        ticker=ticker,
        session_date=_date.fromisoformat(day_prefix),
        open=open_val,
        high=agg["high"],
        low=agg["low"],
        close=latest["last"],
        volume=agg["volume"],
        source=_INTRADAY_SOURCE,
    )


def _with_intraday_overlay(ticker: str, bars: list[DailyBar]) -> list[DailyBar]:
    """Prepend today's intraday bar when it's missing from `bars`.

    `bars` comes back newest-first. If the newest already covers today,
    the sikafinance historique fetch has caught up (post-close) and we
    leave things alone."""
    today = session_date_for()
    if bars and bars[0].session_date >= today:
        return bars
    intraday = _todays_intraday_bar(ticker)
    if intraday is None:
        return bars
    return [intraday, *bars]


def get_history(ticker: str, country: str | None = None) -> list[DailyBar]:
    """Return daily bars newest-first, hitting cache -> DB -> network.

    Indices are served from `index_levels` (no OHLCV, close = level) and
    never trigger a per-ticker network fetch. Equities get an intraday
    overlay from `quote_snapshots` on top of whichever base series we
    return, so today's in-flight candle shows up on the chart even when
    sikafinance historique still stops at yesterday.
    """
    ticker = ticker.upper()
    now = time.time()

    with _lock:
        cached = _cache.get(ticker)
    if cached and now - cached[0] < TTL_S:
        log.debug("history cache hit ticker=%s age=%.1fs", ticker, now - cached[0])
        return _with_intraday_overlay(ticker, cached[1])

    kind = _security_kind(ticker)
    if kind == "index":
        bars = _load_index_levels(ticker)
        with _lock:
            _cache[ticker] = (now, bars)
        return bars  # index chart is close-only; no intraday overlay yet
    if kind == "bond":
        # Bonds get their close from the brvm.org daily poll, not
        # sikafinance historique (which 404s on bond tickers). Serve
        # daily_bars directly and skip the network fetch fallback.
        bars = _load_from_db(ticker)
        with _lock:
            _cache[ticker] = (now, bars)
        return bars

    # Cache miss: try DB first — no I/O if the newest bar is younger
    # than 2*TTL. `_newest_ingested_age` already returns seconds elapsed
    # since ingest, so it's compared against the freshness threshold
    # directly (an earlier `now - age` was always ≈ time.time() and
    # never fell inside the window — a latent bug the overlay work
    # surfaced).
    db_bars = _load_from_db(ticker)
    if db_bars and _newest_ingested_age(ticker) < TTL_S * 2:
        with _lock:
            _cache[ticker] = (now, db_bars)
        return _with_intraday_overlay(ticker, db_bars)

    # Cold or stale — refetch from sikafinance.
    country = country or _security_country(ticker)
    try:
        fetched = sikafinance.fetch_historique(ticker, country)
    except Exception as e:
        log.warning("historique fetch failed for %s: %s; returning DB rows", ticker, e)
        with _lock:
            _cache[ticker] = (now, db_bars)
        return _with_intraday_overlay(ticker, db_bars)

    if fetched:
        with connect(_db_path()) as conn:
            quotes_repo.upsert_daily_bars(conn, fetched)
        bars = fetched
    else:
        # Empty fetch (network up but sikafinance returned no rows —
        # off-hours weirdness, temporary outage on the historique
        # endpoint). Keep the DB rows rather than blanking the chart.
        bars = db_bars
    with _lock:
        _cache[ticker] = (now, bars)
    return _with_intraday_overlay(ticker, bars)


def _newest_ingested_age(ticker: str) -> float:
    """Seconds since the newest daily bar for `ticker` was ingested. If no
    bars exist, return a large value so the caller treats it as stale."""
    from datetime import datetime

    with connect(_db_path()) as conn:
        r = conn.execute(
            "SELECT MAX(ingested_utc) FROM daily_bars WHERE ticker = ?", (ticker,)
        ).fetchone()
    if not r or not r[0]:
        return 10**9
    ts = r[0].replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return 10**9
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(tz=UTC) - dt).total_seconds()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


# --------------------------------------------------------------------------
# Bulk history backfill
# --------------------------------------------------------------------------


def backfill_all(
    client: httpx.Client | None = None,
    *,
    min_age_days: int = 7,
    delay_between_requests_s: float = 0.5,
) -> dict[str, int]:
    """Walk every active equity, fetch sikafinance historique, upsert
    into `daily_bars`.

    Previously `daily_bars` only got populated when a user visited the
    Chart tab — so 30/48 equities had zero history and the Directory's
    period columns (1W/1M/3M/1Y/ALL) rendered "—" for them. This
    backfill closes that gap.

    Skips tickers whose newest ingested bar is under `min_age_days` old
    so a rerun during the week is cheap. `delay_between_requests_s` is
    the polite pause between historique fetches (same shape as
    `filings.pull_all`)."""
    close = client is None
    client = client or make_client()
    counts = {
        "considered": 0,
        "fetched": 0,
        "up_to_date": 0,
        "no_rows": 0,
        "failed": 0,
        "bars_inserted": 0,
    }
    max_age_s = min_age_days * 86400

    db_path = _db_path()
    try:
        with connect(db_path) as conn:
            equities = list(conn.execute(
                "SELECT ticker, country FROM securities "
                "WHERE kind = 'equity' AND active = 1 "
                "ORDER BY ticker"
            ).fetchall())
        counts["considered"] = len(equities)

        for row in equities:
            ticker = row["ticker"]
            country = row["country"]

            # Skip when we've already fetched recently — a full pass over
            # 48 equities at 0.5s each is ~24s of network + polite waits,
            # cheap enough to rerun but not free.
            if _newest_ingested_age(ticker) < max_age_s:
                counts["up_to_date"] += 1
                continue

            try:
                bars = sikafinance.fetch_historique(ticker, country, client=client)
            except httpx.HTTPError as e:
                log.warning("history-backfill: %s failed: %s", ticker, e)
                counts["failed"] += 1
                continue

            if not bars:
                counts["no_rows"] += 1
                continue

            with connect(db_path) as conn:
                quotes_repo.upsert_daily_bars(conn, bars)
            counts["fetched"] += 1
            counts["bars_inserted"] += len(bars)

            if delay_between_requests_s:
                time.sleep(delay_between_requests_s)
    finally:
        if close:
            client.close()

    # A fresh backfill invalidates the per-ticker in-memory cache so the
    # next chart render picks up the new bars without waiting for TTL.
    clear_cache()

    log.info("history backfill: %s", counts)
    return counts
