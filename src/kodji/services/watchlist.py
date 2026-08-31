"""Watchlist CRUD on top of the store + latest-snapshot join for the UI."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from kodji.config import settings
from kodji.db import connect
from kodji.services._view import QuoteRow, WatchlistView
from kodji.store import watchlists as repo


class WatchlistNotFound(LookupError):
    pass


class TickerUnknown(LookupError):
    pass


class WatchlistExists(ValueError):
    """Raised by `create()` when the new name slugifies onto an existing
    watchlist. Callers surface this as a validation error rather than
    letting the `UNIQUE (slug)` IntegrityError blow through to the TUI
    input handler (F-06)."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(slug)


def _db_path() -> Path:
    return Path(settings.db_path)


def list_all() -> list[WatchlistView]:
    with connect(_db_path()) as conn:
        rows = repo.list_all(conn)
        return [
            WatchlistView(
                id=r["id"],
                slug=r["slug"],
                name=r["name"],
                created_utc=r["created_utc"],
            )
            for r in rows
        ]


def create(name: str) -> WatchlistView:
    slug = repo.slugify(name)
    with connect(_db_path()) as conn:
        # Pre-check the slug: cheap, race-free enough for a single-user
        # tool, and lets us raise a typed error instead of catching an
        # IntegrityError string. A truly concurrent creation still hits
        # the IntegrityError branch below.
        if repo.get_by_slug(conn, slug) is not None:
            raise WatchlistExists(slug)
        try:
            wl_id = repo.create(conn, name, slug=slug)
        except sqlite3.IntegrityError as e:
            raise WatchlistExists(slug) from e
        row = conn.execute("SELECT * FROM watchlists WHERE id = ?", (wl_id,)).fetchone()
    return WatchlistView(
        id=row["id"], slug=row["slug"], name=row["name"], created_utc=row["created_utc"]
    )


def get_with_quotes(slug: str) -> WatchlistView:
    with connect(_db_path()) as conn:
        wl = repo.get_by_slug(conn, slug)
        if wl is None:
            raise WatchlistNotFound(slug)
        # F-38: bond tickers have no `quote_snapshots` row — their
        # prices land in `daily_bars` (clean price) and
        # `bond_snapshots` (accrued coupon). Without a fallback, bond
        # watchlist rows rendered permanent em-dashes even when the
        # exchange page had a live price. `latest_bond_bar` picks the
        # newest close per bond ticker so the quote board can degrade
        # gracefully.
        rows = conn.execute(
            """
            WITH latest_snap AS (
                SELECT ticker, MAX(captured_utc) AS captured_utc
                FROM quote_snapshots
                GROUP BY ticker
            ),
            latest_bond_bar AS (
                SELECT ticker, MAX(session_date) AS session_date
                FROM daily_bars
                GROUP BY ticker
            )
            SELECT s.ticker, s.name, s.country, s.kind,
                   qs.last, qs.change_pct, qs.volume, qs.turnover,
                   qs.captured_utc,
                   db.close  AS bond_close,
                   db.volume AS bond_volume,
                   db.turnover AS bond_turnover,
                   db.ingested_utc AS bond_ingested_utc
            FROM watchlist_items wi
            JOIN securities s USING (ticker)
            LEFT JOIN latest_snap ls USING (ticker)
            LEFT JOIN quote_snapshots qs
                ON qs.ticker = ls.ticker AND qs.captured_utc = ls.captured_utc
            LEFT JOIN latest_bond_bar lb
                ON lb.ticker = s.ticker AND s.kind = 'bond'
            LEFT JOIN daily_bars db
                ON db.ticker = lb.ticker AND db.session_date = lb.session_date
            WHERE wi.watchlist_id = ?
            ORDER BY wi.sort_order, wi.added_utc
            """,
            (wl["id"],),
        ).fetchall()
    items = []
    for r in rows:
        # Equities read `quote_snapshots`; bonds fall back to
        # `daily_bars`. `change_pct` is intentionally None for bond
        # rows — the exchange doesn't publish a bond-side day-change
        # % and computing one from a two-day daily_bars diff would be
        # misleading vs. equities' true intraday %.
        if r["kind"] == "bond":
            last = r["bond_close"]
            change_pct = None
            volume = r["bond_volume"]
            turnover = r["bond_turnover"]
            captured_utc = r["bond_ingested_utc"]
        else:
            last = r["last"]
            change_pct = r["change_pct"]
            volume = r["volume"]
            turnover = r["turnover"]
            captured_utc = r["captured_utc"]
        items.append(
            QuoteRow(
                ticker=r["ticker"],
                name=r["name"],
                country=r["country"],
                last=last,
                change_pct=change_pct,
                volume=volume,
                turnover=turnover,
                captured_utc=captured_utc,
            )
        )
    return WatchlistView(
        id=wl["id"],
        slug=wl["slug"],
        name=wl["name"],
        created_utc=wl["created_utc"],
        items=items,
    )


def add_item(slug: str, ticker: str) -> None:
    ticker = ticker.upper().strip()
    with connect(_db_path()) as conn:
        wl = repo.get_by_slug(conn, slug)
        if wl is None:
            raise WatchlistNotFound(slug)
        known = conn.execute(
            "SELECT 1 FROM securities WHERE ticker = ?", (ticker,)
        ).fetchone()
        if not known:
            raise TickerUnknown(ticker)
        repo.add_item(conn, wl["id"], ticker)


def remove_item(slug: str, ticker: str) -> None:
    ticker = ticker.upper().strip()
    with connect(_db_path()) as conn:
        wl = repo.get_by_slug(conn, slug)
        if wl is None:
            raise WatchlistNotFound(slug)
        repo.remove_item(conn, wl["id"], ticker)


def delete(slug: str) -> None:
    with connect(_db_path()) as conn:
        n = repo.delete(conn, slug)
    if n == 0:
        raise WatchlistNotFound(slug)
