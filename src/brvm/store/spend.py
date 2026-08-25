"""SQLite repository for the daily API spend counters.

One row per UTC day per table. Amounts accumulate in micro-dollars
because a single Haiku call can cost far less than a cent; `usd_cents`
is kept as a rounded mirror so the table stays readable in `sqlite3`.

Four tables share this shape:
- `llm_spend` — news-tagging (Phase 3b), $1/day cap.
- `filings_spend` — fundamentals extractor (Phase 4b), $2/day cap.
- `brief_spend` — daily brief writer (Phase 6b), $0.50/day cap.
- `note_spend` — analyst-notes writer (Phase 6c), $3/day cap.

`table` is a `Literal` of the known names so the SQL builders can't
be pointed at arbitrary tables via caller input.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Literal

MICROS_PER_USD = 1_000_000
MICROS_PER_CENT = MICROS_PER_USD // 100

SpendTable = Literal["llm_spend", "filings_spend", "brief_spend", "note_spend"]


def _day_str(day: date | str | None) -> str:
    if day is None:
        from brvm.clock import utcnow

        return utcnow().date().isoformat()
    return day if isinstance(day, str) else day.isoformat()


def get_day(
    conn: sqlite3.Connection,
    day: date | str | None = None,
    *,
    table: SpendTable = "llm_spend",
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {table} WHERE day = ?", (_day_str(day),)
    ).fetchone()


def spent_micros(
    conn: sqlite3.Connection,
    day: date | str | None = None,
    *,
    table: SpendTable = "llm_spend",
) -> int:
    row = get_day(conn, day, table=table)
    return int(row["usd_micros"]) if row else 0


def remaining_micros(
    conn: sqlite3.Connection,
    cap_cents: int,
    day: date | str | None = None,
    *,
    table: SpendTable = "llm_spend",
) -> int:
    """Budget headroom left for `day`, floored at 0."""
    return max(0, cap_cents * MICROS_PER_CENT - spent_micros(conn, day, table=table))


def add_usage(
    conn: sqlite3.Connection,
    *,
    input_tokens: int,
    output_tokens: int,
    usd_micros: int,
    calls: int = 1,
    day: date | str | None = None,
    table: SpendTable = "llm_spend",
) -> int:
    """Accumulate one (or more) API calls onto the day's row.

    Returns the day's new total in micro-dollars.
    """
    d = _day_str(day)
    conn.execute(
        f"""
        INSERT INTO {table} (day, calls, input_tokens, output_tokens, usd_cents, usd_micros)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(day) DO UPDATE SET
            calls         = calls + excluded.calls,
            input_tokens  = input_tokens + excluded.input_tokens,
            output_tokens = output_tokens + excluded.output_tokens,
            usd_micros    = usd_micros + excluded.usd_micros,
            usd_cents     = (usd_micros + excluded.usd_micros + ?) / ?
        """,
        (
            d,
            calls,
            input_tokens,
            output_tokens,
            round(usd_micros / MICROS_PER_CENT),
            usd_micros,
            MICROS_PER_CENT // 2,  # +half a cent => integer division rounds to nearest
            MICROS_PER_CENT,
        ),
    )
    conn.commit()
    return spent_micros(conn, d, table=table)


def recent(
    conn: sqlite3.Connection,
    *,
    limit: int = 14,
    table: SpendTable = "llm_spend",
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            f"SELECT * FROM {table} ORDER BY day DESC LIMIT ?", (limit,)
        ).fetchall()
    )
