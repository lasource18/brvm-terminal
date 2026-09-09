"""Apply SQL migrations in migrations/ to the SQLite DB at $DB_PATH.

Idempotency model: the `_schema_migrations` tracking table is the
authoritative record. A migration whose id is present in that table
is skipped without re-executing. SQLite lacks `ALTER TABLE ... ADD
COLUMN IF NOT EXISTS`, so the `ALTER` migrations (0004 onward) cannot
carry their own idempotency guards — they rely entirely on the
tracking table. Consequences (F-40):
  * Do NOT hand-execute a migration file outside `apply_all` unless
    you also insert the row into `_schema_migrations` in the same
    transaction. A half-applied ALTER + no tracker row would raise
    `duplicate column name` on the next `just migrate`.
  * `CREATE TABLE` / `CREATE INDEX` migrations should still carry
    `IF NOT EXISTS` guards as a defensive belt — they're free.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the src layout importable when run as `python scripts/migrate.py`.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kodji.config import settings  # noqa: E402
from kodji.db import (  # noqa: E402
    connect,
    ensure_migrations_table,
    pending_migrations,
)

MIGRATIONS_DIR = ROOT / "migrations"


def applied_ids(conn) -> set[str]:
    rows = conn.execute("SELECT id FROM _schema_migrations").fetchall()
    return {r[0] for r in rows}


def apply_all() -> None:
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[migrate] db={db_path}")

    with connect(db_path) as conn:
        ensure_migrations_table(conn)
        already = applied_ids(conn)
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not files:
            print("[migrate] no migration files found")
            return
        for f in files:
            mid = f.stem
            if mid in already:
                print(f"[migrate] skip  {mid}")
                continue
            print(f"[migrate] apply {mid}")
            conn.executescript(f.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO _schema_migrations(id, applied_utc) VALUES (?, datetime('now'))",
                (mid,),
            )
            conn.commit()
    print("[migrate] done")


def check() -> int:
    """`--check`: report drift without touching the DB.

    For a deploy script that wants the answer before restarting the
    service. Exit 1 when something is pending, so `python scripts/
    migrate.py --check && systemctl restart kodji` reads correctly.
    """
    pending = pending_migrations(settings.db_path, MIGRATIONS_DIR)
    if not pending:
        print(f"[migrate] up to date ({settings.db_path})")
        return 0
    print(f"[migrate] {len(pending)} pending: {', '.join(pending)}")
    return 1


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        raise SystemExit(check())
    apply_all()
