"""On-demand OHLCV history service with a 15-minute in-memory TTL cache.

Miss path: fetch sikafinance historique, upsert into `daily_bars`, hydrate
the cache. Hit path: return the cached list. If the cache is warm but the
DB has fresher rows (e.g. from a nightly batch we might add later), the
cache is bypassed and rebuilt from the DB.
"""

from __future__ import annotations

import time
from datetime import UTC
from pathlib import Path
from threading import Lock

from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.models import DailyBar
from brvm.sources import sikafinance
from brvm.store import quotes as quotes_repo

_INDEX_SOURCE = "index_levels"

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


def get_history(ticker: str, country: str | None = None) -> list[DailyBar]:
    """Return daily bars newest-first, hitting cache -> DB -> network.

    Indices are served from `index_levels` (no OHLCV, close = level) and
    never trigger a per-ticker network fetch.
    """
    ticker = ticker.upper()
    now = time.time()

    with _lock:
        cached = _cache.get(ticker)
    if cached and now - cached[0] < TTL_S:
        log.debug("history cache hit ticker=%s age=%.1fs", ticker, now - cached[0])
        return cached[1]

    if _security_kind(ticker) == "index":
        bars = _load_index_levels(ticker)
        with _lock:
            _cache[ticker] = (now, bars)
        return bars

    # Cache miss: try DB first — no I/O if we already have anything.
    db_bars = _load_from_db(ticker)
    if db_bars and now - _newest_ingested_age(ticker) < TTL_S * 2:
        with _lock:
            _cache[ticker] = (now, db_bars)
        return db_bars

    # Cold or stale — refetch from sikafinance.
    country = country or _security_country(ticker)
    try:
        bars = sikafinance.fetch_historique(ticker, country)
    except Exception as e:
        log.warning("historique fetch failed for %s: %s; returning DB rows", ticker, e)
        with _lock:
            _cache[ticker] = (now, db_bars)
        return db_bars

    if bars:
        with connect(_db_path()) as conn:
            quotes_repo.upsert_daily_bars(conn, bars)
    with _lock:
        _cache[ticker] = (now, bars)
    return bars


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
