from unittest.mock import patch

from fastapi.testclient import TestClient


def _mocked_client() -> TestClient:
    """TestClient that doesn't actually spin up the APScheduler in tests."""
    from brvm.apps.web.main import app

    with patch("brvm.apps.web.main.build_scheduler") as bs:
        sched = bs.return_value
        sched.get_jobs.return_value = []
        yield_client = TestClient(app)
        return yield_client


def test_health_endpoint():
    client = _mocked_client()
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
