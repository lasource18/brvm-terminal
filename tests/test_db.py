from pathlib import Path

from kodji.db import connect, ensure_migrations_table


def test_migration_creates_all_tables(tmp_db_path: Path):
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "0001_init.sql").read_text()
    with connect(tmp_db_path) as conn:
        ensure_migrations_table(conn)
        conn.executescript(sql)
        conn.commit()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    expected = {
        "_schema_migrations",
        "securities",
        "quote_snapshots",
        "daily_bars",
        "index_levels",
        "fetch_log",
    }
    assert expected <= tables


def test_wal_mode_and_fk(tmp_db_path: Path):
    with connect(tmp_db_path) as conn:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert journal.lower() == "wal"
    assert fk == 1
