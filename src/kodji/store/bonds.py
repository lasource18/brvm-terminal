"""SQLite repository for `bond_snapshots` + bond-specific reads on `securities`."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date

from kodji.clock import utc_iso
from kodji.models import BondSnapshot


def upsert_snapshots(conn: sqlite3.Connection, snaps: Iterable[BondSnapshot]) -> int:
    now = utc_iso()
    n = 0
    for s in snaps:
        conn.execute(
            """
            INSERT INTO bond_snapshots
                (ticker, session_date, accrued_coupon, last_coupon_date,
                 last_coupon_amount, source, ingested_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, session_date) DO UPDATE SET
                accrued_coupon     = excluded.accrued_coupon,
                last_coupon_date   = excluded.last_coupon_date,
                last_coupon_amount = excluded.last_coupon_amount,
                source             = excluded.source,
                ingested_utc       = excluded.ingested_utc
            """,
            (
                s.ticker,
                s.session_date.isoformat(),
                s.accrued_coupon,
                s.last_coupon_date.isoformat() if s.last_coupon_date else None,
                s.last_coupon_amount,
                s.source,
                now,
            ),
        )
        n += 1
    conn.commit()
    return n


def latest_snapshot(conn: sqlite3.Connection, ticker: str) -> BondSnapshot | None:
    row = conn.execute(
        """
        SELECT ticker, session_date, accrued_coupon, last_coupon_date,
               last_coupon_amount, source
        FROM bond_snapshots WHERE ticker = ?
        ORDER BY session_date DESC LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if row is None:
        return None
    return BondSnapshot(
        ticker=row["ticker"],
        session_date=date.fromisoformat(row["session_date"]),
        accrued_coupon=row["accrued_coupon"],
        last_coupon_date=(
            date.fromisoformat(row["last_coupon_date"])
            if row["last_coupon_date"] else None
        ),
        last_coupon_amount=row["last_coupon_amount"],
        source=row["source"],
    )


def list_by_issuer(
    conn: sqlite3.Connection, issuer_name: str, *, exclude_ticker: str | None = None
) -> list[sqlite3.Row]:
    """All active bonds from a given issuer, sorted by maturity year.

    `exclude_ticker` skips the current bond so the caller can render "other
    bonds from this issuer" without a self-row. Matches on exact issuer
    name — the parser normalizes ("SOCIAL BOND CRRH-UEMOA" → "CRRH-UEMOA")
    so the grouping doesn't split legitimate siblings.
    """
    clauses = ["kind = 'bond'", "active = 1", "issuer_name = ?"]
    params: list = [issuer_name]
    if exclude_ticker:
        clauses.append("ticker != ?")
        params.append(exclude_ticker)
    where = " AND ".join(clauses)
    return list(
        conn.execute(
            f"""
            SELECT ticker, name, coupon_rate, maturity_year, issue_date,
                   country, sector
            FROM securities
            WHERE {where}
            ORDER BY maturity_year IS NULL, maturity_year ASC, ticker ASC
            """,
            params,
        ).fetchall()
    )


