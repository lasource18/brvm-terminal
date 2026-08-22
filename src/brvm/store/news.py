"""SQLite repository for news_items + corporate_actions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date, timedelta

from brvm.clock import utc_iso
from brvm.models import CorporateAction, NewsItem


def upsert_news_items(conn: sqlite3.Connection, items: Iterable[NewsItem]) -> tuple[int, int]:
    """Insert new news rows keyed on url_hash. Existing rows are left alone
    (dedupe on ingest — LLM-tagged fields must not be overwritten).

    Returns (inserted, skipped_dupes).
    """
    now = utc_iso()
    inserted = 0
    skipped = 0
    for it in items:
        cur = conn.execute(
            """
            INSERT INTO news_items
                (source, kind, url, url_hash, title, chapeau, issuer_name,
                 ticker_hint, published_at, fetched_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url_hash) DO NOTHING
            """,
            (
                it.source,
                it.kind,
                it.url,
                it.url_hash,
                it.title,
                it.chapeau,
                it.issuer_name,
                it.ticker_hint,
                it.published_at,
                now,
            ),
        )
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    return inserted, skipped


def upsert_corporate_actions(
    conn: sqlite3.Connection, items: Iterable[CorporateAction]
) -> tuple[int, int]:
    """Insert new corporate actions; refresh amount/yield/pay_date/note on
    an existing (ticker, kind, ex_date) row. Returns (inserted, updated).

    Note: the UNIQUE constraint on (ticker, kind, ex_date) does not
    dedupe rows with ex_date=NULL (SQLite treats NULLs as distinct in
    UNIQUE). We handle that here with an IS NULL existence check so
    "A préciser" dividends don't accumulate on every poll.
    """
    now = utc_iso()
    inserted = 0
    updated = 0
    for a in items:
        ex_iso = a.ex_date.isoformat() if a.ex_date else None
        if ex_iso is None:
            row = conn.execute(
                "SELECT id FROM corporate_actions "
                "WHERE ticker = ? AND kind = ? AND ex_date IS NULL",
                (a.ticker, a.kind),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM corporate_actions "
                "WHERE ticker = ? AND kind = ? AND ex_date = ?",
                (a.ticker, a.kind, ex_iso),
            ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO corporate_actions
                    (ticker, kind, ex_date, pay_date, amount, currency,
                     yield_pct, note, source, source_url,
                     first_seen_utc, last_seen_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    a.ticker, a.kind, ex_iso,
                    a.pay_date.isoformat() if a.pay_date else None,
                    a.amount, a.currency, a.yield_pct, a.note,
                    a.source, a.source_url, now, now,
                ),
            )
            inserted += 1
        else:
            conn.execute(
                """
                UPDATE corporate_actions SET
                    pay_date      = COALESCE(?, pay_date),
                    amount        = COALESCE(?, amount),
                    currency      = COALESCE(?, currency),
                    yield_pct     = COALESCE(?, yield_pct),
                    note          = COALESCE(?, note),
                    source_url    = COALESCE(?, source_url),
                    last_seen_utc = ?
                WHERE id = ?
                """,
                (
                    a.pay_date.isoformat() if a.pay_date else None,
                    a.amount, a.currency, a.yield_pct, a.note,
                    a.source_url, now, row["id"],
                ),
            )
            updated += 1
    conn.commit()
    return inserted, updated


def _news_filter_clause(
    *,
    ticker: str | None,
    category: str | None,
    date_from: str | None,
    date_to: str | None,
    min_relevance: int | None,
    source: str | None,
) -> tuple[str, list[object]]:
    """Shared WHERE builder for list_news + count_news so a filter change
    can never make the count and list disagree."""
    where: list[str] = []
    params: list[object] = []
    if ticker:
        where.append(
            "(ticker_hint = ? OR (tickers_llm IS NOT NULL "
            "AND (',' || tickers_llm || ',') LIKE ?))"
        )
        params.extend([ticker, f"%,{ticker},%"])
    if category:
        where.append("category_llm = ?")
        params.append(category)
    if source:
        where.append("source = ?")
        params.append(source)
    if date_from:
        # Compare on the same expression the ORDER BY uses so items with no
        # `published_at` fall back to their fetch time.
        where.append("COALESCE(published_at, fetched_utc) >= ?")
        params.append(date_from)
    if date_to:
        where.append("COALESCE(published_at, fetched_utc) <= ?")
        # Inclusive end-of-day so a `date_to='YYYY-MM-DD'` matches all rows
        # from that day regardless of the time component in ISO-8601.
        params.append(date_to + "T23:59:59Z" if len(date_to) == 10 else date_to)
    if min_relevance is not None:
        where.append("relevance IS NOT NULL AND relevance >= ?")
        params.append(min_relevance)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    return where_sql, params


def list_news(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_relevance: int | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    where_sql, params = _news_filter_clause(
        ticker=ticker, category=category, date_from=date_from, date_to=date_to,
        min_relevance=min_relevance, source=source,
    )
    params.extend([limit, offset])
    return list(
        conn.execute(
            f"""
            SELECT * FROM news_items
            {where_sql}
            ORDER BY COALESCE(published_at, fetched_utc) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    )


def count_news(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_relevance: int | None = None,
    source: str | None = None,
) -> int:
    where_sql, params = _news_filter_clause(
        ticker=ticker, category=category, date_from=date_from, date_to=date_to,
        min_relevance=min_relevance, source=source,
    )
    return conn.execute(
        f"SELECT COUNT(*) FROM news_items {where_sql}", params
    ).fetchone()[0]


def list_corporate_actions_upcoming(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
    days: int = 30,
    today: date | None = None,
) -> list[sqlite3.Row]:
    """Actions with ex_date in [today, today+days] (or TBD when ticker given)."""
    today = today or date.today()
    end = today + timedelta(days=days)
    where = ["(ex_date IS NULL OR ex_date BETWEEN ? AND ?)"]
    params: list[object] = [today.isoformat(), end.isoformat()]
    if ticker:
        where.append("ticker = ?")
        params.append(ticker)
    return list(
        conn.execute(
            f"""
            SELECT * FROM corporate_actions
            WHERE {' AND '.join(where)}
            ORDER BY (ex_date IS NULL), ex_date, ticker
            """,
            params,
        ).fetchall()
    )


def count_corporate_actions(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0]


def list_untagged(conn: sqlite3.Connection, *, limit: int = 100) -> list[sqlite3.Row]:
    """News rows the LLM tagger hasn't seen yet, newest first.

    `tagged_utc IS NULL` is the only gate — the worker stamps it on every
    item it processes (even low-relevance ones), so an article is never
    re-sent to the API. See the partial index `ix_news_items_untagged`.
    """
    return list(
        conn.execute(
            """
            SELECT id, source, kind, url, title, chapeau, issuer_name,
                   ticker_hint, published_at
            FROM news_items
            WHERE tagged_utc IS NULL
            ORDER BY COALESCE(published_at, fetched_utc) DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def count_untagged(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM news_items WHERE tagged_utc IS NULL"
    ).fetchone()[0]


def apply_tags(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    tickers: Iterable[str] = (),
    relevance: int | None = None,
    category: str | None = None,
    summary_fr: str | None = None,
    summary_en: str | None = None,
    tagged_utc: str | None = None,
    commit: bool = True,
) -> None:
    """Write one item's LLM tags. `tickers` is stored as a CSV string so
    `list_news(ticker=...)` can do a `',' || tickers_llm || ','` LIKE match.
    """
    csv = ",".join(dict.fromkeys(t.strip().upper() for t in tickers if t.strip())) or None
    conn.execute(
        """
        UPDATE news_items SET
            tickers_llm  = ?,
            relevance    = ?,
            category_llm = ?,
            summary_fr   = ?,
            summary_en   = ?,
            tagged_utc   = ?
        WHERE id = ?
        """,
        (csv, relevance, category, summary_fr, summary_en, tagged_utc or utc_iso(), item_id),
    )
    if commit:
        conn.commit()
