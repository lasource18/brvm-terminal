"""SQLite connection factory + migrations helper.

Enables WAL mode + foreign keys on every connection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path))
    try:
        _configure(conn)
        yield conn
    finally:
        conn.close()


def ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _schema_migrations (
            id           TEXT PRIMARY KEY,
            applied_utc  TEXT NOT NULL
        )
        """
    )
    conn.commit()
