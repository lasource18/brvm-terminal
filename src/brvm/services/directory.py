"""Full securities directory with filters."""

from __future__ import annotations

from pathlib import Path

from brvm.config import settings
from brvm.db import connect
from brvm.services._view import DirectoryRow


def _db_path() -> Path:
    return Path(settings.db_path)


def distinct_countries() -> list[str]:
    with connect(_db_path()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT country FROM securities "
            "WHERE country IS NOT NULL AND active = 1 ORDER BY country"
        ).fetchall()
    return [r["country"] for r in rows]


def distinct_sectors() -> list[str]:
    with connect(_db_path()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT sector FROM securities "
            "WHERE sector IS NOT NULL AND sector != '' AND active = 1 ORDER BY sector"
        ).fetchall()
    return [r["sector"] for r in rows]


def list_directory(
    country: str | None = None,
    sector: str | None = None,
    q: str | None = None,
    kind: str | None = None,
) -> list[DirectoryRow]:
    clauses = ["s.active = 1"]
    params: list = []
    if country:
        clauses.append("UPPER(s.country) = ?")
        params.append(country.upper())
    if sector:
        clauses.append("s.sector = ?")
        params.append(sector)
    if kind:
        clauses.append("s.kind = ?")
        params.append(kind)
    if q:
        clauses.append("(UPPER(s.ticker) LIKE ? OR UPPER(s.name) LIKE ?)")
        like = f"%{q.upper()}%"
        params.extend([like, like])
    where = " AND ".join(clauses)

    sql = f"""
    WITH latest AS (
        SELECT ticker, MAX(captured_utc) AS captured_utc
        FROM quote_snapshots
        GROUP BY ticker
    )
    SELECT s.ticker, s.name, s.kind, s.country, s.sector,
           qs.last, qs.change_pct
    FROM securities s
    LEFT JOIN latest l USING (ticker)
    LEFT JOIN quote_snapshots qs
        ON qs.ticker = l.ticker AND qs.captured_utc = l.captured_utc
    WHERE {where}
    ORDER BY s.kind DESC, s.country IS NULL, s.country, s.ticker
    """
    with connect(_db_path()) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        DirectoryRow(
            ticker=r["ticker"],
            name=r["name"],
            kind=r["kind"],
            country=r["country"],
            sector=r["sector"],
            last=r["last"],
            change_pct=r["change_pct"],
        )
        for r in rows
    ]
