"""SQLite repository for extracted fundamentals (Phase 4b).

Three tables, all keyed on `(ticker, period_year, period_kind)`:

- `financials`         — the P&L + balance-sheet core
- `financial_segments` — business / geographic breakdown
- `ownership`          — shareholder register

A re-extraction of the same period atomically replaces the prior rows, so
the extractor never has to reason about diffs. `filing_id` on every row is
the audit trail back to the source PDF; if that filing is later withdrawn
the whole triple can be dropped by joining on it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from brvm.clock import utc_iso


@dataclass(frozen=True)
class FinancialsRow:
    ticker: str
    period_year: int
    period_kind: str = "annual"
    currency: str = "XOF"
    revenue: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    total_assets: float | None = None
    total_equity: float | None = None
    eps: float | None = None
    dividend_per_share: float | None = None


@dataclass(frozen=True)
class SegmentRow:
    name: str
    segment_kind: str  # 'business' | 'geo'
    revenue: float | None = None
    share_pct: float | None = None


@dataclass(frozen=True)
class OwnershipRow:
    holder: str
    share_pct: float | None = None
    shares: int | None = None


def replace_period(
    conn: sqlite3.Connection,
    *,
    filing_id: int,
    financials: FinancialsRow,
    segments: Iterable[SegmentRow] = (),
    ownership: Iterable[OwnershipRow] = (),
) -> None:
    """Atomically replace one `(ticker, period_year, period_kind)` triple.

    Deletes any prior rows for the same key before inserting so a re-run
    of the extractor (schema tweak, corrected PDF) can't leave stale
    segment / ownership entries around.
    """
    now = utc_iso()
    key = (financials.ticker, financials.period_year, financials.period_kind)

    conn.execute(
        "DELETE FROM financials WHERE ticker = ? AND period_year = ? AND period_kind = ?",
        key,
    )
    conn.execute(
        "DELETE FROM financial_segments WHERE ticker = ? AND period_year = ? AND period_kind = ?",
        key,
    )
    conn.execute(
        "DELETE FROM ownership WHERE ticker = ? AND period_year = ? AND period_kind = ?",
        key,
    )

    conn.execute(
        """
        INSERT INTO financials
            (ticker, period_year, period_kind, currency,
             revenue, operating_income, net_income, total_assets,
             total_equity, eps, dividend_per_share, filing_id, extracted_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            financials.ticker,
            financials.period_year,
            financials.period_kind,
            financials.currency,
            financials.revenue,
            financials.operating_income,
            financials.net_income,
            financials.total_assets,
            financials.total_equity,
            financials.eps,
            financials.dividend_per_share,
            filing_id,
            now,
        ),
    )

    # Dedupe segments / ownership on their PK components — the model
    # occasionally repeats a row (e.g. "Autres" appearing twice) and the
    # composite PK would otherwise raise mid-batch.
    seen_seg: set[tuple[str, str]] = set()
    for s in segments:
        if not s.name:
            continue
        seg_key = (s.segment_kind, s.name)
        if seg_key in seen_seg:
            continue
        seen_seg.add(seg_key)
        conn.execute(
            """
            INSERT INTO financial_segments
                (ticker, period_year, period_kind, segment_kind, name,
                 revenue, share_pct, filing_id, extracted_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                financials.ticker,
                financials.period_year,
                financials.period_kind,
                s.segment_kind,
                s.name,
                s.revenue,
                s.share_pct,
                filing_id,
                now,
            ),
        )

    seen_holder: set[str] = set()
    for o in ownership:
        if not o.holder or o.holder in seen_holder:
            continue
        seen_holder.add(o.holder)
        conn.execute(
            """
            INSERT INTO ownership
                (ticker, period_year, period_kind, holder,
                 share_pct, shares, filing_id, extracted_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                financials.ticker,
                financials.period_year,
                financials.period_kind,
                o.holder,
                o.share_pct,
                o.shares,
                filing_id,
                now,
            ),
        )
    conn.commit()


def list_financials(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    period_kind: str = "annual",
    limit: int = 6,
) -> list[sqlite3.Row]:
    """Recent periods first, capped at `limit`. Used by the Financials tab
    to render a 5-year table with the most recent column on the left."""
    return list(
        conn.execute(
            """
            SELECT * FROM financials
            WHERE ticker = ? AND period_kind = ?
            ORDER BY period_year DESC
            LIMIT ?
            """,
            (ticker, period_kind, limit),
        ).fetchall()
    )


def latest_period(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    period_kind: str = "annual",
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM financials
        WHERE ticker = ? AND period_kind = ?
        ORDER BY period_year DESC
        LIMIT 1
        """,
        (ticker, period_kind),
    ).fetchone()


def list_segments(
    conn: sqlite3.Connection,
    ticker: str,
    period_year: int,
    *,
    period_kind: str = "annual",
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM financial_segments
            WHERE ticker = ? AND period_year = ? AND period_kind = ?
            ORDER BY segment_kind, share_pct DESC NULLS LAST, name
            """,
            (ticker, period_year, period_kind),
        ).fetchall()
    )


def list_ownership(
    conn: sqlite3.Connection,
    ticker: str,
    period_year: int,
    *,
    period_kind: str = "annual",
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM ownership
            WHERE ticker = ? AND period_year = ? AND period_kind = ?
            ORDER BY share_pct DESC NULLS LAST, holder
            """,
            (ticker, period_year, period_kind),
        ).fetchall()
    )
