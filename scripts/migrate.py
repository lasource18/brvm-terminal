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

from brvm.config import settings  # noqa: E402
from brvm.db import connect, ensure_migrations_table  # noqa: E402

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


if __name__ == "__main__":
    apply_all()
