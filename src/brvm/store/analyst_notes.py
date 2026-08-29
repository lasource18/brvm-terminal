"""SQLite repository for weekly per-ticker analyst notes (Phase 6c).

Rerunning the generator for the same `(ticker, week_start)` overwrites
the row in one statement so the UI never renders a half-written note
mid-swap.
"""

from __future__ import annotations

import sqlite3

from brvm.clock import utc_iso
from brvm.models import AnalystNote


def _row_to_note(r: sqlite3.Row) -> AnalystNote:
    keys = r.keys()
    return AnalystNote(
        ticker=r["ticker"],
        week_start=r["week_start"],
        model=r["model"],
        title=r["title"],
        markdown=r["markdown"],
        markdown_fr=r["markdown_fr"] if "markdown_fr" in keys else None,
        translation_generated_utc=(
            r["translation_generated_utc"] if "translation_generated_utc" in keys else None
        ),
        context_json=r["context_json"],
        input_tokens=r["input_tokens"],
        output_tokens=r["output_tokens"],
        usd_micros=r["usd_micros"],
        generated_utc=r["generated_utc"],
    )


def upsert(conn: sqlite3.Connection, note: AnalystNote) -> None:
    """Insert or replace the row for `(ticker, week_start)`. Sets
    `generated_utc` to now() when not provided so the store owns the
    timestamp.

    PR-I: `markdown_fr` + `translation_generated_utc` ride along in the
    same statement so the note writer can persist both atomically."""
    conn.execute(
        """
        INSERT OR REPLACE INTO analyst_notes
            (ticker, week_start, model, title, markdown, markdown_fr,
             translation_generated_utc, context_json,
             input_tokens, output_tokens, usd_micros, generated_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            note.ticker,
            note.week_start,
            note.model,
            note.title,
            note.markdown,
            note.markdown_fr,
            note.translation_generated_utc,
            note.context_json,
            note.input_tokens,
            note.output_tokens,
            note.usd_micros,
            note.generated_utc or utc_iso(),
        ),
    )
    conn.commit()


def set_translation(
    conn: sqlite3.Connection,
    ticker: str,
    week_start: str,
    markdown_fr: str,
    *,
    generated_utc: str | None = None,
) -> bool:
    """Attach or refresh the FR translation for `(ticker, week_start)`.

    Idempotent — the source markdown is untouched. Returns True when a
    row matched, False when no note exists yet (translator racing ahead
    of the generator — logged, not raised)."""
    cur = conn.execute(
        "UPDATE analyst_notes "
        "SET markdown_fr = ?, translation_generated_utc = ? "
        "WHERE ticker = ? AND week_start = ?",
        (markdown_fr, generated_utc or utc_iso(), ticker, week_start),
    )
    conn.commit()
    return cur.rowcount > 0


def get(conn: sqlite3.Connection, ticker: str, week_start: str) -> AnalystNote | None:
    r = conn.execute(
        "SELECT * FROM analyst_notes WHERE ticker = ? AND week_start = ?",
        (ticker, week_start),
    ).fetchone()
    return _row_to_note(r) if r else None


def latest_for_ticker(conn: sqlite3.Connection, ticker: str) -> AnalystNote | None:
    r = conn.execute(
        "SELECT * FROM analyst_notes WHERE ticker = ? "
        "ORDER BY week_start DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return _row_to_note(r) if r else None


def list_for_ticker(
    conn: sqlite3.Connection, ticker: str, *, limit: int = 12
) -> list[AnalystNote]:
    """Newest-first archive for the tab's sidebar."""
    return [
        _row_to_note(r)
        for r in conn.execute(
            "SELECT * FROM analyst_notes WHERE ticker = ? "
            "ORDER BY week_start DESC LIMIT ?",
            (ticker, limit),
        ).fetchall()
    ]


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM analyst_notes").fetchone()[0]


def count_for_week(conn: sqlite3.Connection, week_start: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM analyst_notes WHERE week_start = ?",
        (week_start,),
    ).fetchone()[0]
