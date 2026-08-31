"""Global search over securities (ticker + name)."""

from __future__ import annotations

from pathlib import Path

from kodji.config import settings
from kodji.db import connect
from kodji.services._view import SearchHit


def _db_path() -> Path:
    return Path(settings.db_path)


def search(q: str, limit: int = 10) -> list[SearchHit]:
    """Case-insensitive search: exact ticker beats prefix beats name LIKE."""
    q = (q or "").strip()
    if not q:
        return []
    q_up = q.upper()
    q_pref = f"{q_up}%"
    q_like = f"%{q_up}%"
    q_name = f"%{q}%"
    with connect(_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT ticker, name, kind, country
            FROM securities
            WHERE active = 1
              AND (
                UPPER(ticker) LIKE ?
                OR UPPER(name) LIKE ?
              )
            ORDER BY
              (UPPER(ticker) = ?) DESC,
              (UPPER(ticker) LIKE ?) DESC,
              (UPPER(name)   LIKE ?) DESC,
              ticker
            LIMIT ?
            """,
            (q_like, q_like, q_up, q_pref, q_name, limit),
        ).fetchall()
    return [
        SearchHit(ticker=r["ticker"], name=r["name"], kind=r["kind"], country=r["country"])
        for r in rows
    ]
