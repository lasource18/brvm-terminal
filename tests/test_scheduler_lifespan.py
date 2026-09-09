from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from kodji.db import connect
from tests.conftest import apply_migrations, reset_module_state


def test_lifespan_starts_and_stops_scheduler(monkeypatch, tmp_path: Path):
    # Own migrated DB: the lifespan now asserts the schema is current
    # before starting the scheduler, so this test can't ride on whatever
    # DB_PATH the previous test left in the settings cache.
    db_path = tmp_path / "kodji.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_module_state()
    with connect(db_path) as conn:
        apply_migrations(conn)

    from kodji.apps.web.main import app

    with patch("kodji.apps.web.main.build_scheduler") as bs:
        sched = MagicMock()
        sched.get_jobs.return_value = []
        bs.return_value = sched
        with TestClient(app) as c:
            r = c.get("/health")
            assert r.status_code == 200

    assert sched.start.call_count == 1
    assert sched.shutdown.call_count == 1
    reset_module_state()
