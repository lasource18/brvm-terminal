"""SQLite connection factory + migrations helpers.

Enables WAL mode + foreign keys on every connection, and owns the
schema-drift check the app entry points run at startup.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kodji.logging import get

log = get(__name__)


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


class PendingMigrations(RuntimeError):
    """Raised at startup when the SQL files on disk are ahead of the DB.

    The failure this prevents: a deploy that ships a migration but never
    runs `just migrate` boots fine and then 500s on the first request
    that touches the new column — at whatever hour a user happens to
    open that page. Checking at startup turns a silent, delayed,
    user-facing error into a loud one at the moment of the mistake, when
    the fix is one command away.
    """


def _default_migrations_dir() -> Path | None:
    """Where the .sql files live, or None if they aren't shipped.

    Two candidates: the source checkout (`src/kodji/db.py` →
    `<root>/migrations`, which is how both dev and the systemd unit run,
    since `uv sync` installs the project editable), then the working
    directory as the fallback for anything that copies the tree without
    the src layout. A wheel installed non-editable has no `migrations/`
    at all — hence the None, which callers report rather than reading as
    "nothing pending".
    """
    candidates = (
        Path(__file__).resolve().parents[2] / "migrations",
        Path.cwd() / "migrations",
    )
    return next((c for c in candidates if c.is_dir()), None)


def available_migration_ids(migrations_dir: Path | None = None) -> list[str]:
    """Migration ids on disk, in apply order. Ids are filename stems, the
    same key `scripts/migrate.py` writes into `_schema_migrations`."""
    d = migrations_dir or _default_migrations_dir()
    if d is None:
        return []
    return [f.stem for f in sorted(d.glob("*.sql"))]


def applied_migration_ids(conn: sqlite3.Connection) -> set[str]:
    """Ids recorded in the tracking table.

    A DB that predates the table (or a brand-new file) reads as zero
    applied rather than raising — the caller wants "everything is
    pending", not a crash.
    """
    try:
        rows = conn.execute("SELECT id FROM _schema_migrations").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {r[0] for r in rows}


def pending_migrations(db_path: str | Path, migrations_dir: Path | None = None) -> list[str]:
    """Migration ids present on disk but absent from this DB's tracker.

    Read-only by design: a `db_path` that doesn't exist yet reports every
    migration as pending instead of being created as a side effect of the
    check (`sqlite3.connect` would otherwise leave an empty file behind).
    """
    available = available_migration_ids(migrations_dir)
    if not available:
        return []
    if not Path(db_path).exists():
        return list(available)
    with connect(db_path) as conn:
        applied = applied_migration_ids(conn)
    return [m for m in available if m not in applied]


def assert_schema_current(db_path: str | Path, migrations_dir: Path | None = None) -> None:
    """Refuse to start when the schema is behind the code.

    Deliberately fatal rather than a warning: a warning scrolls past in
    the journal and the app still serves the broken page. The message
    names the pending files and the one command that fixes them.
    """
    if _default_migrations_dir() is None and migrations_dir is None:
        log.warning(
            "migrations/ not found next to the package or in %s — "
            "skipping the pending-migration check",
            Path.cwd(),
        )
        return
    pending = pending_migrations(db_path, migrations_dir)
    if not pending:
        return
    raise PendingMigrations(
        f"{len(pending)} migration(s) not applied to {db_path}: "
        f"{', '.join(pending)}. Run `just migrate` (or "
        "`uv run python scripts/migrate.py`) before starting the app."
    )
