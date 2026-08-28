"""Watchlist CRUD on top of the store + latest-snapshot join for the UI."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from brvm.config import settings
from brvm.db import connect
from brvm.services._view import QuoteRow, WatchlistView
from brvm.store import watchlists as repo


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
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT ticker, MAX(captured_utc) AS captured_utc
                FROM quote_snapshots
                GROUP BY ticker
            )
            SELECT s.ticker, s.name, s.country,
                   qs.last, qs.change_pct, qs.volume, qs.turnover, qs.captured_utc
            FROM watchlist_items wi
            JOIN securities s USING (ticker)
            LEFT JOIN latest l USING (ticker)
            LEFT JOIN quote_snapshots qs
                ON qs.ticker = l.ticker AND qs.captured_utc = l.captured_utc
            WHERE wi.watchlist_id = ?
            ORDER BY wi.sort_order, wi.added_utc
            """,
            (wl["id"],),
        ).fetchall()
    items = [
        QuoteRow(
            ticker=r["ticker"],
            name=r["name"],
            country=r["country"],
            last=r["last"],
            change_pct=r["change_pct"],
            volume=r["volume"],
            turnover=r["turnover"],
            captured_utc=r["captured_utc"],
        )
        for r in rows
    ]
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
