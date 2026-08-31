"""Read-side services powering the overview page + fragments."""

from __future__ import annotations

from pathlib import Path

from kodji.clock import is_market_open, utc_iso
from kodji.config import settings
from kodji.db import connect
from kodji.services._view import IndexTile, Overview, QuoteRow, SecurityView

# Indices we surface in the top strip on the overview page (in this order).
HEADLINE_INDICES = ("BRVMC", "BRVM30", "BRVMPR")


def _db_path() -> Path:
    return Path(settings.db_path)


def _rows_to_quotes(rows) -> list[QuoteRow]:
    return [
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


def _latest_snapshot_join_sql(order_by: str, limit: int, where: str = "") -> str:
    """Return SQL that yields the newest snapshot per equity, filtered/sorted."""
    return f"""
    WITH latest AS (
        SELECT ticker, MAX(captured_utc) AS captured_utc
        FROM quote_snapshots
        GROUP BY ticker
    )
    SELECT s.ticker, s.name, s.country,
           qs.last, qs.change_pct, qs.volume, qs.turnover, qs.captured_utc
    FROM quote_snapshots qs
    JOIN latest l USING (ticker, captured_utc)
    JOIN securities s USING (ticker)
    WHERE s.kind = 'equity' {where}
    ORDER BY {order_by}
    LIMIT {int(limit)}
    """


def top_by_turnover(limit: int = 10) -> list[QuoteRow]:
    with connect(_db_path()) as conn:
        rows = conn.execute(
            _latest_snapshot_join_sql("COALESCE(qs.turnover, 0) DESC", limit)
        ).fetchall()
    return _rows_to_quotes(rows)


def gainers(limit: int = 10) -> list[QuoteRow]:
    with connect(_db_path()) as conn:
        rows = conn.execute(
            _latest_snapshot_join_sql(
                "qs.change_pct DESC",
                limit,
                where="AND qs.change_pct IS NOT NULL AND qs.change_pct > 0",
            )
        ).fetchall()
    return _rows_to_quotes(rows)


def losers(limit: int = 10) -> list[QuoteRow]:
    with connect(_db_path()) as conn:
        rows = conn.execute(
            _latest_snapshot_join_sql(
                "qs.change_pct ASC",
                limit,
                where="AND qs.change_pct IS NOT NULL AND qs.change_pct < 0",
            )
        ).fetchall()
    return _rows_to_quotes(rows)


def indices_tiles(tickers: tuple[str, ...] = HEADLINE_INDICES) -> list[IndexTile]:
    tiles: list[IndexTile] = []
    with connect(_db_path()) as conn:
        for t in tickers:
            row = conn.execute(
                """
                SELECT s.ticker, s.name, il.level, il.change_pct, il.session_date
                FROM securities s
                LEFT JOIN index_levels il ON il.ticker = s.ticker
                  AND il.session_date = (
                      SELECT MAX(session_date) FROM index_levels WHERE ticker = s.ticker
                  )
                WHERE s.ticker = ?
                """,
                (t,),
            ).fetchone()
            if row is None:
                continue
            from datetime import date as _date

            sd = row["session_date"]
            tiles.append(
                IndexTile(
                    ticker=row["ticker"],
                    name=row["name"],
                    level=row["level"],
                    change_pct=row["change_pct"],
                    session_date=_date.fromisoformat(sd) if sd else None,
                )
            )
    return tiles


def last_snapshot_utc() -> str | None:
    with connect(_db_path()) as conn:
        r = conn.execute("SELECT MAX(captured_utc) FROM quote_snapshots").fetchone()
    return r[0] if r and r[0] else None


def overview(limit: int = 10) -> Overview:
    # Local import so the market service stays free of a hard dep on news
    # (keeps import order simple; there's no cycle risk).
    from kodji.services import news as news_svc

    return Overview(
        indices=indices_tiles(),
        gainers=gainers(limit),
        losers=losers(limit),
        turnover_leaders=top_by_turnover(limit),
        upcoming_actions=news_svc.list_upcoming_actions(days=30),
        generated_utc=utc_iso(),
        market_open=is_market_open(),
        last_snapshot_utc=last_snapshot_utc(),
    )


def get_security(ticker: str) -> SecurityView | None:
    ticker = ticker.upper()
    with connect(_db_path()) as conn:
        sec = conn.execute(
            "SELECT * FROM securities WHERE ticker = ?", (ticker,)
        ).fetchone()
        if sec is None:
            return None
        q_row = conn.execute(
            _latest_snapshot_join_sql("qs.captured_utc DESC", 1, where="AND s.ticker = ?"),
            (ticker,),
        ).fetchone()
    quote = None
    if q_row is not None:
        quote = _rows_to_quotes([q_row])[0]
    return SecurityView(
        ticker=sec["ticker"],
        name=sec["name"],
        kind=sec["kind"],
        country=sec["country"],
        isin=sec["isin"],
        source_url=sec["source_url"],
        quote=quote,
    )
