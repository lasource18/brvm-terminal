"""PR-Z: billing over HTTP — pricing buttons, checkout, return, webhook."""

from __future__ import annotations

import json

import httpx
import pytest

from kodji.config import reset_settings_cache, settings
from kodji.db import connect
from kodji.services import billing
from kodji.services.mailer import ConsoleMailer
from kodji.store import accounts as accounts_repo
from kodji.store import payments as payments_repo
from kodji.store.accounts import DEFAULT_ACCOUNT_ID

EMAIL = "trader@example.ci"
LINK = "https://checkout.flutterwave.test/pay/abc"


@pytest.fixture
def outbox(monkeypatch):
    mailer = ConsoleMailer()
    monkeypatch.setattr("kodji.services.auth.get_mailer", lambda: mailer)
    return mailer


def _sign_in(client, outbox, email: str = EMAIL) -> None:
    client.post("/login", data={"email": email})
    text = outbox.sent[-1].text
    link = next(w for w in text.split() if "/login/t/" in w)
    client.post(link[link.index("/login/t/") :])


@pytest.fixture
def billing_on(monkeypatch):
    monkeypatch.setenv("FLW_PUBLIC_KEY", "FLWPUBK_TEST-x")
    monkeypatch.setenv("FLW_SECRET_KEY", "FLWSECK_TEST-secret")
    monkeypatch.setenv("FLW_WEBHOOK_HASH", "hash-123")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://kodji.test")
    reset_settings_cache()


class _Fake:
    def __init__(self) -> None:
        self.verified: dict = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/payments"):
            return httpx.Response(200, json={"status": "success", "data": {"link": LINK}})
        return httpx.Response(200, json={"status": "success", "data": self.verified})


@pytest.fixture
def fake(monkeypatch):
    """Route every FlutterwaveClient the service builds through the fake."""
    f = _Fake()
    real = billing.FlutterwaveClient

    def factory(secret_key, **kw):
        kw.setdefault("client", httpx.Client(transport=httpx.MockTransport(f.handler)))
        return real(secret_key, **kw)

    monkeypatch.setattr(billing, "FlutterwaveClient", factory)
    return f


def _free_default():
    with connect(settings.db_path) as conn:
        accounts_repo.set_plan(conn, DEFAULT_ACCOUNT_ID, "free")


# --- pricing page -----------------------------------------------------------


def test_pricing_without_keys_says_checkout_is_closed(client):
    _free_default()
    r = client.get("/pricing")
    assert r.status_code == 200
    assert "Checkout is not open yet" in r.text
    assert 'action="/billing/checkout"' not in r.text
    assert "12 000 XOF" in r.text and "120 000 XOF" in r.text


def test_pricing_signed_out_with_keys_points_to_sign_in(client, billing_on):
    _free_default()
    r = client.get("/pricing")
    assert "Sign in to subscribe" in r.text
    assert 'action="/billing/checkout"' not in r.text


def test_pricing_signed_in_with_keys_shows_both_buttons(client, billing_on, outbox):
    _sign_in(client, outbox)
    r = client.get("/pricing")
    assert r.text.count('action="/billing/checkout"') == 2
    assert 'name="period" value="month"' in r.text
    assert 'name="period" value="year"' in r.text
    assert "Pay 1 month" in r.text and "Pay 12 months" in r.text
    assert "no automatic renewal" in r.text


# --- checkout ---------------------------------------------------------------


def test_checkout_requires_a_session(client, billing_on, fake):
    r = client.post("/billing/checkout", data={"period": "month"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert fake.requests == []


def test_checkout_redirects_to_the_hosted_page(client, billing_on, fake, outbox):
    _sign_in(client, outbox)
    r = client.post("/billing/checkout", data={"period": "year"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == LINK
    body = json.loads(fake.requests[0].content)
    assert body["amount"] == "120000"
    assert body["currency"] == "XOF"
    assert body["customer"]["email"] == EMAIL
    assert body["redirect_url"] == "https://kodji.test/billing/return"


def test_checkout_refuses_cross_origin_and_bad_period(client, billing_on, fake, outbox):
    _sign_in(client, outbox)
    r = client.post(
        "/billing/checkout",
        data={"period": "month"},
        headers={"Origin": "https://evil.test"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    r = client.post("/billing/checkout", data={"period": "decade"}, follow_redirects=False)
    assert r.status_code == 400
    assert fake.requests == []


def test_checkout_when_billing_is_closed(client, outbox):
    _sign_in(client, outbox)
    r = client.post("/billing/checkout", data={"period": "month"}, follow_redirects=False)
    assert r.status_code == 503
    assert "Checkout unavailable" in r.text


# --- return -----------------------------------------------------------------


def _start(client, fake, period="month") -> str:
    r = client.post("/billing/checkout", data={"period": period}, follow_redirects=False)
    assert r.status_code == 303
    return json.loads(fake.requests[-1].content)["tx_ref"]


def test_return_activates_and_shows_the_period_end(client, billing_on, fake, outbox):
    _sign_in(client, outbox)
    tx_ref = _start(client, fake)
    fake.verified = {
        "id": 55,
        "tx_ref": tx_ref,
        "amount": 12000,
        "currency": "XOF",
        "status": "successful",
        "payment_type": "mobilemoneyfranco",
        "flw_ref": "F",
    }
    r = client.get(f"/billing/return?status=successful&tx_ref={tx_ref}&transaction_id=55")
    assert r.status_code == 200
    assert "Payment received" in r.text
    assert "Your paid plan is active" in r.text
    # Paid features now open for this session.
    assert client.get("/pricing").text.count("Extend by") == 2
    assert "Active until" in client.get("/pricing").text


def test_return_reports_pending_failed_and_unknown(client, billing_on, fake, outbox, monkeypatch):
    _sign_in(client, outbox)
    tx_ref = _start(client, fake)
    fake.verified = {
        "id": 55,
        "tx_ref": tx_ref,
        "amount": 12000,
        "currency": "XOF",
        "status": "pending",
    }
    monkeypatch.setattr(billing.time, "sleep", lambda s: None)
    r = client.get(f"/billing/return?status=pending&tx_ref={tx_ref}")
    assert r.status_code == 200 and "Payment in progress" in r.text
    # The pending page re-checks itself against the same URL.
    assert 'http-equiv="refresh"' in r.text and f"tx_ref={tx_ref}" in r.text

    fake.verified["status"] = "failed"
    r = client.get(f"/billing/return?status=failed&tx_ref={tx_ref}")
    assert r.status_code == 402 and "Payment not completed" in r.text

    r = client.get("/billing/return?status=successful&tx_ref=kodji-1-month-unknown")
    assert r.status_code == 402 and "could not be verified" in r.text

    r = client.get("/billing/return", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pricing"


def test_return_after_cancelling_on_the_hosted_page(client, billing_on, fake, outbox):
    """Flutterwave sends the customer back with status=cancelled and no
    transaction id; the provider has nothing to verify, and the page must
    say "not completed", not "could not be verified"."""
    _sign_in(client, outbox)
    tx_ref = _start(client, fake)
    calls = len(fake.requests)
    r = client.get(f"/billing/return?status=cancelled&tx_ref={tx_ref}")
    assert r.status_code == 402 and "Payment not completed" in r.text
    assert len(fake.requests) == calls  # no verify call
    with connect(settings.db_path) as conn:
        assert payments_repo.get_by_tx_ref(conn, tx_ref)["status"] == "failed"


# --- webhook ----------------------------------------------------------------


def test_webhook_needs_the_secret_hash(client, billing_on, fake):
    body = json.dumps({"event": "charge.completed", "data": {"tx_ref": "kodji-1-month-x", "id": 1}})
    assert client.post("/billing/webhook", content=body).status_code == 401
    assert (
        client.post("/billing/webhook", content=body, headers={"verif-hash": "nope"}).status_code
        == 401
    )
    assert fake.requests == []


def test_webhook_activates_without_a_browser(client, billing_on, fake, outbox):
    _sign_in(client, outbox)
    tx_ref = _start(client, fake)
    fake.verified = {
        "id": 77,
        "tx_ref": tx_ref,
        "amount": 12000,
        "currency": "XOF",
        "status": "successful",
        "payment_type": "card",
        "flw_ref": "F",
    }
    body = json.dumps(
        {"event": "charge.completed", "data": {"id": 77, "tx_ref": tx_ref, "status": "successful"}}
    )
    # No cookie, a foreign Origin: neither matters for a server-to-server call.
    r = client.post(
        "/billing/webhook",
        content=body,
        headers={"verif-hash": "hash-123", "Origin": "https://flutterwave.com"},
    )
    assert r.status_code == 200 and r.text == "ok"
    assert fake.requests[-1].url.path == "/v3/transactions/77/verify"
    with connect(settings.db_path) as conn:
        row = payments_repo.get_by_tx_ref(conn, tx_ref)
    assert row["status"] == "successful"


# --- billing page -----------------------------------------------------------


def test_billing_page_lists_payments(client, billing_on, fake, outbox):
    assert client.get("/billing", follow_redirects=False).status_code == 303
    _sign_in(client, outbox)
    tx_ref = _start(client, fake, "year")
    fake.verified = {
        "id": 9,
        "tx_ref": tx_ref,
        "amount": 120000,
        "currency": "XOF",
        "status": "successful",
        "payment_type": "card",
        "flw_ref": "F",
    }
    client.get(f"/billing/return?status=successful&tx_ref={tx_ref}&transaction_id=9")
    r = client.get("/billing")
    assert r.status_code == 200
    assert "120 000 XOF" in r.text
    assert tx_ref in r.text
    assert "successful" in r.text
    assert "active until" in r.text


def test_scheduler_registers_the_billing_jobs():
    from kodji.jobs.scheduler import build_scheduler

    ids = {j.id for j in build_scheduler().get_jobs()}
    assert {"billing_expire_hourly", "billing_remind_daily"} <= ids
