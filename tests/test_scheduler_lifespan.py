from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def test_lifespan_starts_and_stops_scheduler():
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
