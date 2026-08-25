"""SQLite repository for the daily briefs (Phase 6b).

Rerunning the generator for the same UTC day overwrites the row — this
is "the brief for 2026-08-24", not an append-only log. A prior run's
row is replaced in one statement so the UI never renders a
half-written brief mid-swap.
"""

from __future__ import annotations

import sqlite3

from brvm.clock import utc_iso
from brvm.models import Brief


def _row_to_brief(r: sqlite3.Row) -> Brief:
    return Brief(
        day=r["day"],
        model=r["model"],
        title=r["title"],
        markdown=r["markdown"],
        context_json=r["context_json"],
        input_tokens=r["input_tokens"],
        output_tokens=r["output_tokens"],
        usd_micros=r["usd_micros"],
        generated_utc=r["generated_utc"],
        session_date=r["session_date"],
    )


def upsert(conn: sqlite3.Connection, brief: Brief) -> None:
    """Insert or replace the row for `brief.day`. Sets `generated_utc`
    to now() when not provided so the store owns the timestamp."""
    conn.execute(
        """
        INSERT OR REPLACE INTO briefs
            (day, model, title, markdown, context_json,
             input_tokens, output_tokens, usd_micros,
             generated_utc, session_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            brief.day,
            brief.model,
            brief.title,
            brief.markdown,
            brief.context_json,
            brief.input_tokens,
            brief.output_tokens,
            brief.usd_micros,
            brief.generated_utc or utc_iso(),
            brief.session_date,
        ),
    )
    conn.commit()


def get(conn: sqlite3.Connection, day: str) -> Brief | None:
    r = conn.execute("SELECT * FROM briefs WHERE day = ?", (day,)).fetchone()
    return _row_to_brief(r) if r else None


def latest(conn: sqlite3.Connection) -> Brief | None:
    r = conn.execute(
        "SELECT * FROM briefs ORDER BY day DESC LIMIT 1"
    ).fetchone()
    return _row_to_brief(r) if r else None


def list_recent(conn: sqlite3.Connection, *, limit: int = 30) -> list[Brief]:
    return [
        _row_to_brief(r)
        for r in conn.execute(
            "SELECT * FROM briefs ORDER BY day DESC LIMIT ?", (limit,)
        ).fetchall()
    ]


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0]
