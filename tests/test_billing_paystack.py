"""PR-Z: the Paystack adapter.

What is pinned: the wire contract (XOF in hundredths on the way out and
back, `reference` as our tx_ref, channels), the status mapping, HMAC
webhook authentication, and that the provider a payment was issued with
is the one that verifies it even after BILLING_PROVIDER changes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from kodji.config import reset_settings_cache, settings
from kodji.db import connect
from kodji.services import billing
from kodji.services.billing import PaystackClient, confirm, start_checkout
from kodji.store import accounts as accounts_repo
from kodji.store import payments as payments_repo

from .conftest import apply_migrations

EMAIL = "trader@example.ci"
API = "https://api.paystack.test"
SECRET = "sk_test_secret"


def at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).replace(tzinfo=UTC)


@pytest.fixture
def db(monkeypatch, tmp_db_path: Path):
    monkeypatch.setenv("DB_PATH", str(tmp_db_path))
    monkeypatch.setenv("BILLING_PROVIDER", "paystack")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", SECRET)
    monkeypatch.setenv("PAYSTACK_PUBLIC_KEY", "pk_test_x")
    monkeypatch.setenv("PAYSTACK_API_BASE", API)
    monkeypatch.setenv("FLW_SECRET_KEY", "")
    monkeypatch.setenv("FLW_PUBLIC_KEY", "")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://kodji.test")
    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setenv("EMAIL_FROM", "")
    reset_settings_cache()
    with connect(tmp_db_path) as conn:
        apply_migrations(conn)
        _, account_id = accounts_repo.ensure_user_with_account(conn, EMAIL)
    yield tmp_db_path, account_id
    reset_settings_cache()


class FakePaystack:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.verified: dict = {}
        self.link = "https://checkout.paystack.test/abc123"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/transaction/initialize"):
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "message": "Authorization URL created",
                    "data": {
                        "authorization_url": self.link,
                        "access_code": "abc123",
                        "reference": json.loads(request.content)["reference"],
                    },
                },
            )
        if "/transaction/verify/" in path:
            if not self.verified:
                return httpx.Response(
                    404, json={"status": False, "message": "Transaction reference not found"}
                )
            return httpx.Response(
                200,
                json={"status": True, "message": "Verification successful", "data": self.verified},
            )
        return httpx.Response(404, json={"status": False, "message": "no route"})

    def client(self) -> PaystackClient:
        return PaystackClient(
            SECRET, base_url=API, client=httpx.Client(transport=httpx.MockTransport(self.handler))
        )


def _paid(reference: str, amount_subunit: int, /, **over) -> dict:
    d = {
        "id": 6549282930,
        "domain": "test",
        "status": "success",
        "reference": reference,
        "receipt_number": None,
        "amount": amount_subunit,
        "gateway_response": "Approved",
        "paid_at": "2026-09-11T22:31:00.000Z",
        "created_at": "2026-09-11T22:30:41.000Z",
        "channel": "mobile_money",
        "currency": "XOF",
        "requested_amount": amount_subunit,
        "customer": {"email": EMAIL, "phone": "+2250700000000"},
        "authorization": {"channel": "mobile_money", "mobile_money_number": "0700000000"},
    }
    d.update(over)
    return d


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()


# --- checkout ---------------------------------------------------------------


def test_initialize_sends_hundredths_reference_and_channels(db):
    path, account_id = db
    ps = FakePaystack()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=ps.client()
    )

    assert out.link == ps.link
    assert out.provider == "paystack"
    req = ps.requests[0]
    assert req.method == "POST" and req.url.path == "/transaction/initialize"
    assert req.headers["authorization"] == f"Bearer {SECRET}"
    body = json.loads(req.content)
    assert body["amount"] == 1_200_000  # 12 000 XOF x 100
    assert body["currency"] == "XOF"
    assert body["reference"] == out.tx_ref
    assert body["email"] == EMAIL
    assert body["callback_url"] == "https://kodji.test/billing/return"
    assert body["channels"] == ["card", "mobile_money"]
    assert body["metadata"] == {"account_id": str(account_id), "period": "month"}

    with connect(path) as conn:
        row = payments_repo.get_by_tx_ref(conn, out.tx_ref)
    assert row["provider"] == "paystack"
    assert row["amount_xof"] == 12_000  # francs in our books


def test_configured_provider_is_used_when_none_is_injected(db, monkeypatch):
    _, account_id = db
    ps = FakePaystack()
    real = billing.PaystackClient
    monkeypatch.setattr(
        billing,
        "PaystackClient",
        lambda secret, **kw: real(
            secret, base_url=API, client=httpx.Client(transport=httpx.MockTransport(ps.handler))
        ),
    )
    out = start_checkout(account_id, EMAIL, "year", base_url="https://kodji.test")
    assert out.provider == "paystack"
    assert json.loads(ps.requests[0].content)["amount"] == 12_000_000


# --- verify -----------------------------------------------------------------


def test_verify_divides_back_to_francs_and_activates(db):
    path, account_id = db
    ps = FakePaystack()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=ps.client()
    )
    ps.verified = _paid(out.tx_ref, 1_200_000)
    res = confirm(out.tx_ref, client=ps.client(), now=at("2026-09-11T22:31:30"))

    assert res.outcome == "activated"
    assert res.period_end_utc == "2026-10-11T22:31:30Z"
    assert ps.requests[-1].url.path == f"/transaction/verify/{out.tx_ref}"
    with connect(path) as conn:
        row = payments_repo.get_by_tx_ref(conn, out.tx_ref)
        sub = accounts_repo.get_subscription(conn, account_id)
    assert row["status"] == "successful"
    assert row["provider_tx_id"] == "6549282930"
    assert row["payment_type"] == "mobile_money"
    assert sub["provider"] == "paystack"
    raw = json.loads(row["raw_json"])
    assert raw["amount"] == 1_200_000 and "customer" not in raw and "authorization" not in raw


def test_verify_refuses_short_payment_in_francs(db):
    path, account_id = db
    ps = FakePaystack()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=ps.client()
    )
    ps.verified = _paid(out.tx_ref, 1_199_900)  # 11 999 XOF
    res = confirm(out.tx_ref, client=ps.client())
    assert res.outcome == "mismatch" and res.note == "amount"
    with connect(path) as conn:
        assert accounts_repo.plan_for(conn, account_id) == "free"


@pytest.mark.parametrize(
    "status, outcome",
    [
        ("abandoned", "failed"),
        ("failed", "failed"),
        ("reversed", "failed"),
        ("ongoing", "pending"),
        ("processing", "pending"),
    ],
)
def test_status_mapping(db, status, outcome):
    _, account_id = db
    ps = FakePaystack()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=ps.client()
    )
    ps.verified = _paid(out.tx_ref, 1_200_000, status=status)
    assert confirm(out.tx_ref, client=ps.client()).outcome == outcome


def test_reference_not_found_is_pending(db):
    _, account_id = db
    ps = FakePaystack()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=ps.client()
    )
    res = confirm(out.tx_ref, client=ps.client())  # ps.verified empty → 404
    assert res.outcome == "pending" and res.note == "not_yet_recorded"


def test_payment_verifies_with_the_provider_that_issued_it(db, monkeypatch):
    """A Paystack checkout must still verify with Paystack after the
    operator flips BILLING_PROVIDER back to Flutterwave."""
    _, account_id = db
    ps = FakePaystack()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=ps.client()
    )
    ps.verified = _paid(out.tx_ref, 1_200_000)

    monkeypatch.setenv("BILLING_PROVIDER", "flutterwave")
    monkeypatch.setenv("FLW_SECRET_KEY", "FLWSECK_TEST-x")
    monkeypatch.setenv("FLW_PUBLIC_KEY", "FLWPUBK_TEST-x")
    reset_settings_cache()
    real = billing.PaystackClient
    monkeypatch.setattr(
        billing,
        "PaystackClient",
        lambda secret, **kw: real(
            secret, base_url=API, client=httpx.Client(transport=httpx.MockTransport(ps.handler))
        ),
    )
    assert settings.billing_provider == "flutterwave"
    assert confirm(out.tx_ref).outcome == "activated"
    assert ps.requests[-1].url.path == f"/transaction/verify/{out.tx_ref}"


# --- webhook ----------------------------------------------------------------


def _event(reference: str, **over) -> bytes:
    data = _paid(reference, 1_200_000, **over)
    return json.dumps({"event": "charge.success", "data": data}).encode()


def test_webhook_requires_a_valid_hmac(db):
    ps = FakePaystack()
    body = _event("kodji-1-month-x")
    assert (
        billing.handle_webhook({}, body, provider="paystack", client=ps.client()).accepted is False
    )
    bad = {"x-paystack-signature": _sign(body, "sk_test_other")}
    assert (
        billing.handle_webhook(bad, body, provider="paystack", client=ps.client()).accepted is False
    )
    tampered = {"x-paystack-signature": _sign(body)}
    assert (
        billing.handle_webhook(
            tampered, body + b" ", provider="paystack", client=ps.client()
        ).accepted
        is False
    )
    assert ps.requests == []


def test_webhook_verifies_and_activates(db):
    path, account_id = db
    ps = FakePaystack()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=ps.client()
    )
    ps.verified = _paid(out.tx_ref, 1_200_000)
    body = _event(out.tx_ref)
    res = billing.handle_webhook(
        {"X-Paystack-Signature": _sign(body)}, body, provider="paystack", client=ps.client()
    )
    assert res.accepted and res.action == "verified"
    assert res.confirmation.outcome == "activated"
    assert ps.requests[-1].url.path == f"/transaction/verify/{out.tx_ref}"
    with connect(path) as conn:
        assert accounts_repo.plan_for(conn, account_id) == "paid"


def test_webhook_ignores_other_events(db):
    ps = FakePaystack()
    body = json.dumps(
        {"event": "transfer.success", "data": {"reference": "kodji-1-month-x"}}
    ).encode()
    res = billing.handle_webhook(
        {"x-paystack-signature": _sign(body)}, body, provider="paystack", client=ps.client()
    )
    assert res.accepted and res.action == "ignored"
    assert ps.requests == []


# --- over HTTP --------------------------------------------------------------


@pytest.fixture
def outbox(monkeypatch):
    from kodji.services.mailer import ConsoleMailer

    mailer = ConsoleMailer()
    monkeypatch.setattr("kodji.services.auth.get_mailer", lambda: mailer)
    return mailer


@pytest.fixture
def paystack_on(monkeypatch):
    monkeypatch.setenv("BILLING_PROVIDER", "paystack")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", SECRET)
    monkeypatch.setenv("PAYSTACK_PUBLIC_KEY", "pk_test_x")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://kodji.test")
    reset_settings_cache()
    ps = FakePaystack()
    real = billing.PaystackClient
    monkeypatch.setattr(
        billing,
        "PaystackClient",
        lambda secret, **kw: real(
            secret, base_url=API, client=httpx.Client(transport=httpx.MockTransport(ps.handler))
        ),
    )
    monkeypatch.setattr(billing.time, "sleep", lambda s: None)
    return ps


def _sign_in(client, outbox):
    client.cookies.clear()
    client.post("/login", data={"email": EMAIL})
    text = outbox.sent[-1].text
    link = next(w for w in text.split() if "/login/t/" in w)
    client.post(link[link.index("/login/t/") :])


def test_return_page_reads_paystacks_reference_params(client, paystack_on, outbox):
    _sign_in(client, outbox)
    r = client.post("/billing/checkout", data={"period": "year"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == paystack_on.link
    tx_ref = json.loads(paystack_on.requests[-1].content)["reference"]
    paystack_on.verified = _paid(tx_ref, 12_000_000)
    r = client.get(f"/billing/return?trxref={tx_ref}&reference={tx_ref}")
    assert r.status_code == 200 and "Payment received" in r.text


def test_provider_webhook_urls(client, paystack_on, outbox):
    _sign_in(client, outbox)
    client.post("/billing/checkout", data={"period": "month"}, follow_redirects=False)
    tx_ref = json.loads(paystack_on.requests[-1].content)["reference"]
    paystack_on.verified = _paid(tx_ref, 1_200_000)
    body = _event(tx_ref)

    assert client.post("/billing/webhook/paystack", content=body).status_code == 401
    r = client.post(
        "/billing/webhook/paystack", content=body, headers={"x-paystack-signature": _sign(body)}
    )
    assert r.status_code == 200
    # The bare URL follows BILLING_PROVIDER, which is paystack here.
    r = client.post("/billing/webhook", content=body, headers={"x-paystack-signature": _sign(body)})
    assert r.status_code == 200
    # Flutterwave's URL still exists but has no hash configured → 401.
    assert client.post("/billing/webhook/flutterwave", content=body).status_code == 401
    assert client.post("/billing/webhook/stripe", content=body).status_code == 404
    with connect(settings.db_path) as conn:
        assert payments_repo.get_by_tx_ref(conn, tx_ref)["status"] == "successful"
