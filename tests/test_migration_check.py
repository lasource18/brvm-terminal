"""Startup schema-drift guard (db.assert_schema_current).

Covers the failure this was written for: code that ships a migration
onto a DB where it was never applied must fail at boot, not on the
first request that touches the new column.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodji.db import (
    PendingMigrations,
    applied_migration_ids,
    assert_schema_current,
    available_migration_ids,
    connect,
    ensure_migrations_table,
    pending_migrations,
)

REAL_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture
def fake_migrations(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "0001_a.sql").write_text("CREATE TABLE IF NOT EXISTS a (x INTEGER);")
    (d / "0002_b.sql").write_text("CREATE TABLE IF NOT EXISTS b (x INTEGER);")
    return d


def _record(db_path: Path, ids: list[str]) -> None:
    with connect(db_path) as conn:
        ensure_migrations_table(conn)
        for mid in ids:
            conn.execute(
                "INSERT INTO _schema_migrations(id, applied_utc) VALUES (?, datetime('now'))",
                (mid,),
            )
        conn.commit()


def test_available_ids_are_filename_stems_in_order(fake_migrations: Path):
    assert available_migration_ids(fake_migrations) == ["0001_a", "0002_b"]


def test_missing_db_file_reports_everything_pending_without_creating_it(
    tmp_path: Path, fake_migrations: Path
):
    db = tmp_path / "absent.sqlite"
    assert pending_migrations(db, fake_migrations) == ["0001_a", "0002_b"]
    # The check must not leave an empty DB behind — that would make the
    # next real migrate run against a file it did not create.
    assert not db.exists()


def test_db_without_tracking_table_reads_as_zero_applied(tmp_db_path: Path):
    with connect(tmp_db_path) as conn:
        assert applied_migration_ids(conn) == set()


def test_partially_migrated_db_lists_only_the_gap(tmp_db_path: Path, fake_migrations: Path):
    _record(tmp_db_path, ["0001_a"])
    assert pending_migrations(tmp_db_path, fake_migrations) == ["0002_b"]


def test_assert_raises_and_names_the_pending_file(tmp_db_path: Path, fake_migrations: Path):
    _record(tmp_db_path, ["0001_a"])
    with pytest.raises(PendingMigrations) as e:
        assert_schema_current(tmp_db_path, fake_migrations)
    assert "0002_b" in str(e.value)
    assert "just migrate" in str(e.value)


def test_assert_passes_when_fully_applied(tmp_db_path: Path, fake_migrations: Path):
    _record(tmp_db_path, ["0001_a", "0002_b"])
    assert_schema_current(tmp_db_path, fake_migrations)


def test_db_ahead_of_the_files_is_not_pending(tmp_db_path: Path, fake_migrations: Path):
    """A rollback to older code leaves the tracker with ids we no longer
    ship. That is not schema drift the app can fix by migrating, so it
    must not block startup."""
    _record(tmp_db_path, ["0001_a", "0002_b", "0003_from_the_future"])
    assert pending_migrations(tmp_db_path, fake_migrations) == []
    assert_schema_current(tmp_db_path, fake_migrations)


def test_real_migrations_dir_is_discovered_by_default(tmp_db_path: Path):
    """Guards the `parents[2]` path resolution in `_default_migrations_dir`
    — a src-layout move would otherwise silently disable the check."""
    ids = available_migration_ids()
    assert ids == [f.stem for f in sorted(REAL_MIGRATIONS.glob("*.sql"))]
    assert "0001_init" in ids


def test_web_app_refuses_to_start_on_pending_migrations(monkeypatch, tmp_path: Path):
    """The whole point, end to end: an unmigrated DB must fail the
    lifespan rather than serve pages that 500 later."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from tests.conftest import reset_module_state

    db = tmp_path / "unmigrated.sqlite"
    db.touch()
    monkeypatch.setenv("DB_PATH", str(db))
    reset_module_state()

    from kodji.apps.web.main import app

    with patch("kodji.apps.web.main.build_scheduler") as bs:
        bs.return_value.get_jobs.return_value = []
        with pytest.raises(PendingMigrations), TestClient(app):
            pass  # pragma: no cover - startup raises

    reset_module_state()
