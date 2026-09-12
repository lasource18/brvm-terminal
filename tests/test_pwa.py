"""PR-AA: the PWA shell (manifest, service worker, offline page) and the
push-subscription API behind the /alerts "enable on this device" button."""

from __future__ import annotations

import json

import pytest

from kodji.apps.web._common import STATIC_DIR, STATIC_VERSION
from kodji.config import reset_settings_cache, settings
from kodji.db import connect
from kodji.services.webpush import generate_vapid_keys
from kodji.store import accounts as accounts_repo
from kodji.store import push as push_repo
from kodji.store.accounts import DEFAULT_ACCOUNT_ID

SUB = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc:def",
    "expirationTime": None,
    "keys": {"p256dh": "BPk-public", "auth": "auth-secret"},
}


@pytest.fixture
def push_keys(monkeypatch):
    """VAPID configured, as production is once `just vapid-keygen` ran."""
    private, public = generate_vapid_keys()
    monkeypatch.setenv("VAPID_PUBLIC_KEY", public)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", private)
    reset_settings_cache()
    yield public
    reset_settings_cache()


class TestShell:
    def test_manifest(self, client):
        r = client.get("/manifest.webmanifest")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/manifest+json")
        m = json.loads(r.text)
        assert m["name"] == "Kodji Terminal"
        assert m["display"] == "standalone"
        assert m["start_url"].startswith("/")
        # Every icon the manifest names must exist and be reachable.
        for icon in m["icons"]:
            assert (STATIC_DIR / icon["src"].removeprefix("/static/")).is_file()
            assert client.get(icon["src"]).status_code == 200

    def test_service_worker_is_served_from_the_root(self, client):
        r = client.get("/sw.js")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/javascript")
        assert r.headers["service-worker-allowed"] == "/"
        assert "no-cache" in r.headers["cache-control"]
        assert "__STATIC_V__" not in r.text
        assert f'const VERSION = "{STATIC_VERSION}";' in r.text
        assert "addEventListener(\"push\"" in r.text

    def test_service_worker_never_caches_fragments_or_api(self, client):
        """Guard the design decision: data is never served from cache."""
        body = client.get("/sw.js").text
        assert "/_frag/" not in body
        assert "/api/" not in body.replace("/api/push/subscribe", "")

    def test_offline_page(self, client):
        r = client.get("/offline")
        assert r.status_code == 200
        assert "No connection" in r.text
        assert "Pas de connexion" in client.get("/offline?lang=fr").text

    def test_offline_page_carries_no_identity_or_nav(self, client):
        """The worker precaches this page, so the copy on disk outlives the
        session that fetched it: it must name nobody and link nowhere that
        needs the network."""
        r = client.get("/offline")
        assert "owner@example.ci" in client.get("/").text   # the session is real
        assert "owner@example.ci" not in r.text
        for dead_offline in ('href="/news"', 'href="/alerts"', 'href="/billing"', "search-input"):
            assert dead_offline not in r.text

    def test_pages_link_the_manifest(self, client):
        body = client.get("/").text
        assert '<link rel="manifest" href="/manifest.webmanifest">' in body
        assert '<meta name="theme-color" content="#0b0f14">' in body
        assert 'rel="apple-touch-icon"' in body

    def test_app_js_registers_the_worker(self, client):
        assert 'serviceWorker.register("/sw.js")' in client.get(f"/static/app.js?v={STATIC_VERSION}").text


class TestConfig:
    def test_disabled_without_keys(self, client):
        r = client.get("/api/push/config")
        assert r.json() == {"enabled": False, "public_key": None}

    def test_enabled_with_keys(self, client, push_keys):
        r = client.get("/api/push/config")
        assert r.json() == {"enabled": True, "public_key": push_keys}


class TestSubscribe:
    def test_requires_sign_in(self, client, push_keys):
        client.cookies.clear()
        r = client.post("/api/push/subscribe", json=SUB)
        assert r.status_code == 401
        assert r.json() == {"error": "sign_in_required"}

    def test_refused_on_the_free_plan(self, client, push_keys):
        """Alerts are paid; a free account has nothing to receive."""
        with connect(settings.db_path) as conn:
            accounts_repo.set_plan(conn, DEFAULT_ACCOUNT_ID, "free")
        r = client.post("/api/push/subscribe", json=SUB)
        assert r.status_code == 402
        assert r.json()["error"] == "payment_required"

    def test_refused_without_server_keys(self, client):
        r = client.post("/api/push/subscribe", json=SUB)
        assert r.status_code == 503
        assert r.json() == {"error": "push_not_configured"}

    def test_stores_the_device_for_the_signed_in_user(self, client, push_keys):
        r = client.post("/api/push/subscribe", json=SUB, headers={"User-Agent": "TestPhone/1.0"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "devices": 1}
        with connect(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM push_subscriptions").fetchone()
        assert row["endpoint"] == SUB["endpoint"]
        assert row["p256dh"] == "BPk-public"
        assert row["auth"] == "auth-secret"
        assert row["user_agent"] == "TestPhone/1.0"
        # The operator of the test client, not the account: push is per user.
        with connect(settings.db_path) as conn:
            owner = conn.execute("SELECT id FROM users WHERE email = 'owner@example.ci'").fetchone()
        assert row["user_id"] == owner["id"]

    def test_resubscribing_the_same_endpoint_is_one_device(self, client, push_keys):
        client.post("/api/push/subscribe", json=SUB)
        rotated = {**SUB, "keys": {"p256dh": "BPk-new", "auth": "auth-new"}}
        r = client.post("/api/push/subscribe", json=rotated)
        assert r.json()["devices"] == 1
        with connect(settings.db_path) as conn:
            row = conn.execute("SELECT p256dh, auth FROM push_subscriptions").fetchone()
        assert (row["p256dh"], row["auth"]) == ("BPk-new", "auth-new")

    @pytest.mark.parametrize(
        "body,error",
        [
            ({**SUB, "endpoint": "http://insecure.example/x"}, "bad_endpoint"),
            ({**SUB, "endpoint": ""}, "bad_endpoint"),
            ({"endpoint": SUB["endpoint"]}, "bad_keys"),
            ({**SUB, "keys": {"p256dh": "", "auth": "a"}}, "bad_keys"),
            ("not json", "bad_endpoint"),
        ],
    )
    def test_rejects_malformed_subscriptions(self, client, push_keys, body, error):
        if isinstance(body, str):
            r = client.post("/api/push/subscribe", content=body, headers={"Content-Type": "application/json"})
        else:
            r = client.post("/api/push/subscribe", json=body)
        assert r.status_code == 400
        assert r.json() == {"error": error}
        with connect(settings.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0] == 0

    def test_unsubscribe(self, client, push_keys):
        client.post("/api/push/subscribe", json=SUB)
        r = client.request("DELETE", "/api/push/subscribe", json={"endpoint": SUB["endpoint"]})
        assert r.json() == {"ok": True, "removed": 1, "devices": 0}
        again = client.request("DELETE", "/api/push/subscribe", json={"endpoint": SUB["endpoint"]})
        assert again.json()["removed"] == 0

    def test_unsubscribe_requires_sign_in(self, client, push_keys):
        client.cookies.clear()
        r = client.request("DELETE", "/api/push/subscribe", json={"endpoint": SUB["endpoint"]})
        assert r.status_code == 401

    def test_a_user_cannot_unsubscribe_another_users_device(self, client, push_keys):
        with connect(settings.db_path) as conn:
            conn.execute(
                "INSERT INTO users (email, created_utc) VALUES ('other@example.ci', '2026-01-01')"
            )
            other = conn.execute("SELECT id FROM users WHERE email = 'other@example.ci'").fetchone()["id"]
            conn.commit()
            push_repo.upsert(conn, user_id=other, endpoint="https://push.example/theirs", p256dh="k", auth="a")
        r = client.request("DELETE", "/api/push/subscribe", json={"endpoint": "https://push.example/theirs"})
        assert r.json()["removed"] == 0


class TestAlertsPage:
    def test_offers_the_enable_button_when_configured(self, client, push_keys):
        body = client.get("/alerts").text
        assert 'id="push-enable"' in body
        assert f'data-public-key="{push_keys}"' in body
        assert '<span id="push-devices">0</span>' in body
        assert "not configured" not in body

    def test_counts_this_users_devices(self, client, push_keys):
        client.post("/api/push/subscribe", json=SUB)
        assert '<span id="push-devices">1</span>' in client.get("/alerts").text

    def test_french(self, client, push_keys):
        body = client.get("/alerts?lang=fr").text
        assert "Activer sur cet appareil" in body
        assert "Sur iPhone et iPad" in body
