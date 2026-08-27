"""SQLite repository for the `securities` table."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from brvm.clock import utc_iso
from brvm.models import Security


def upsert(conn: sqlite3.Connection, items: Iterable[Security]) -> int:
    """Insert new securities, or refresh name/country/sector/source_url and
    bump `last_seen_utc` for existing ones. Returns rows touched.

    Bond-specific reference fields (`coupon_rate`, `maturity_year`,
    `issue_date`, `issuer_name`) COALESCE onto the existing row on
    conflict — a parser regression that briefly returns NULL for a
    field shouldn't wipe a value we already had. Correct as a bond's
    reference fields don't change over its lifetime.
    """
    now = utc_iso()
    n = 0
    for s in items:
        conn.execute(
            """
            INSERT INTO securities
                (ticker, isin, name, kind, country, sector, currency,
                 source_url, first_seen_utc, last_seen_utc, active,
                 coupon_rate, maturity_year, issue_date, issuer_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                name          = excluded.name,
                isin          = COALESCE(excluded.isin, securities.isin),
                country       = COALESCE(excluded.country, securities.country),
                sector        = COALESCE(excluded.sector, securities.sector),
                source_url    = COALESCE(excluded.source_url, securities.source_url),
                last_seen_utc = excluded.last_seen_utc,
                active        = 1,
                coupon_rate   = COALESCE(excluded.coupon_rate, securities.coupon_rate),
                maturity_year = COALESCE(excluded.maturity_year, securities.maturity_year),
                issue_date    = COALESCE(excluded.issue_date, securities.issue_date),
                issuer_name   = COALESCE(excluded.issuer_name, securities.issuer_name)
            """,
            (
                s.ticker,
                s.isin,
                s.name,
                s.kind,
                s.country,
                s.sector,
                s.currency,
                s.source_url,
                now,
                now,
                s.coupon_rate,
                s.maturity_year,
                s.issue_date.isoformat() if s.issue_date else None,
                s.issuer_name,
            ),
        )
        n += 1
    conn.commit()
    return n


def count(conn: sqlite3.Connection, kind: str | None = None) -> int:
    if kind:
        return conn.execute(
            "SELECT COUNT(*) FROM securities WHERE kind = ?", (kind,)
        ).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM securities").fetchone()[0]


def list_by_kind(conn: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM securities WHERE kind = ? AND active = 1 ORDER BY ticker",
            (kind,),
        ).fetchall()
    )


def list_missing_sector(
    conn: sqlite3.Connection, kind: str = "equity"
) -> list[sqlite3.Row]:
    """Active securities of the given kind whose sector is NULL or empty."""
    return list(
        conn.execute(
            """
            SELECT ticker, name, country
            FROM securities
            WHERE kind = ?
              AND active = 1
              AND (sector IS NULL OR TRIM(sector) = '')
            ORDER BY ticker
            """,
            (kind,),
        ).fetchall()
    )


def update_sector(conn: sqlite3.Connection, ticker: str, sector: str) -> None:
    conn.execute(
        "UPDATE securities SET sector = ? WHERE ticker = ?", (sector, ticker)
    )
    conn.commit()


# --------------------------------------------------------------------------
# Company facts (Phase 4d — feeds the ratios engine)
# --------------------------------------------------------------------------


def update_company_facts(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    shares_outstanding: int | None = None,
    float_pct: float | None = None,
    market_cap_xof: float | None = None,
    commit: bool = True,
) -> None:
    """Stamp the sikafinance-sourced company facts on `securities`.

    `COALESCE(?, existing)` on every field so a partial refresh (e.g.
    sikafinance stopped publishing `float_pct` for one issuer) doesn't
    clobber a value we already had. `company_facts_refreshed_utc` is
    always bumped so the weekly refresh job can gate on freshness.
    """
    conn.execute(
        """
        UPDATE securities SET
            shares_outstanding = COALESCE(?, shares_outstanding),
            float_pct          = COALESCE(?, float_pct),
            market_cap_xof     = COALESCE(?, market_cap_xof),
            company_facts_refreshed_utc = ?
        WHERE ticker = ?
        """,
        (shares_outstanding, float_pct, market_cap_xof, utc_iso(), ticker),
    )
    if commit:
        conn.commit()


def list_stale_company_facts(
    conn: sqlite3.Connection,
    *,
    max_age_days: int = 7,
    limit: int = 200,
) -> list[sqlite3.Row]:
    """Equity rows whose company facts are missing or older than
    `max_age_days`. Used by the weekly refresh job — one row per active
    equity."""
    return list(
        conn.execute(
            """
            SELECT ticker, name, country
            FROM securities
            WHERE kind = 'equity' AND active = 1
              AND (company_facts_refreshed_utc IS NULL
                   OR julianday('now') - julianday(company_facts_refreshed_utc) > ?)
            ORDER BY ticker
            LIMIT ?
            """,
            (max_age_days, limit),
        ).fetchall()
    )


def get_company_facts(
    conn: sqlite3.Connection, ticker: str
) -> sqlite3.Row | None:
    """Read the four company-facts columns for one ticker. Returns None
    if the ticker isn't in `securities` at all (so the caller can tell
    'no row' apart from 'row exists but not yet refreshed')."""
    return conn.execute(
        """
        SELECT ticker, shares_outstanding, float_pct, market_cap_xof,
               company_facts_refreshed_utc
        FROM securities
        WHERE ticker = ?
        """,
        (ticker,),
    ).fetchone()
