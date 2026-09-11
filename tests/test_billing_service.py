"""PR-Z: Flutterwave billing — the service and the payments repository.

Everything runs against an `httpx.MockTransport` standing in for
api.flutterwave.com, so what is pinned here is the wire contract (what
we send to /payments, what we require back from /verify) and the
activation rules: verify before activate, extend rather than restart,
never activate twice, refuse a mismatch.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from kodji.clock import utc_iso
from kodji.config import reset_settings_cache
from kodji.db import connect
from kodji.services import billing
from kodji.services.billing import PERIODS, FlutterwaveClient, confirm, fmt_xof, start_checkout
from kodji.services.mailer import ConsoleMailer
from kodji.store import accounts as accounts_repo
from kodji.store import payments as payments_repo

from .conftest import apply_migrations

EMAIL = "trader@example.ci"
API = "https://api.flutterwave.test/v3"


def at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).replace(tzinfo=UTC)


@pytest.fixture
def db(monkeypatch, tmp_db_path: Path):
    monkeypatch.setenv("DB_PATH", str(tmp_db_path))
    monkeypatch.setenv("FLW_PUBLIC_KEY", "FLWPUBK_TEST-x")
    monkeypatch.setenv("FLW_SECRET_KEY", "FLWSECK_TEST-secret")
    monkeypatch.setenv("FLW_WEBHOOK_HASH", "hash-123")
    monkeypatch.setenv("FLW_API_BASE", API)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://kodji.test")
    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setenv("EMAIL_FROM", "")
    reset_settings_cache()
    with connect(tmp_db_path) as conn:
        apply_migrations(conn)
        _, account_id = accounts_repo.ensure_user_with_account(conn, EMAIL)
    yield tmp_db_path, account_id
    reset_settings_cache()


class FakeFlutterwave:
    """Records requests; answers /payments with a link and /verify with
    whatever `self.verified` holds."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.verified: dict = {}
        self.link = "https://checkout.flutterwave.test/pay/abc"
        self.fail_create = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/payments"):
            if self.fail_create:
                return httpx.Response(400, json={"status": "error", "message": "nope"})
            return httpx.Response(
                200,
                json={"status": "success", "message": "Hosted Link", "data": {"link": self.link}},
            )
        if "/verify" in path or "verify_by_reference" in path:
            return httpx.Response(
                200, json={"status": "success", "message": "ok", "data": self.verified}
            )
        return httpx.Response(404, json={"status": "error", "message": "no route"})

    def client(self) -> FlutterwaveClient:
        return FlutterwaveClient(
            "FLWSECK_TEST-secret",
            base_url=API,
            client=httpx.Client(transport=httpx.MockTransport(self.handler)),
        )


def _paid_payload(tx_ref: str, amount: int, /, **over) -> dict:
    """Positional-only so a test can override `amount` or `tx_ref` via **over."""
    d = {
        "id": 987654,
        "tx_ref": tx_ref,
        "flw_ref": "FLW-MOCK-1",
        "amount": amount,
        "currency": "XOF",
        "charged_amount": amount,
        "status": "successful",
        "payment_type": "mobilemoneyfranco",
        "created_at": "2026-09-11T10:00:00.000Z",
        "customer": {"email": EMAIL},
    }
    d.update(over)
    return d


# --- catalogue --------------------------------------------------------------


def test_prices_are_integer_francs_from_settings(db):
    assert PERIODS["month"].price_xof == 12_000
    assert PERIODS["year"].price_xof == 120_000
    assert fmt_xof(120_000) == "120 000 XOF"


# --- checkout ---------------------------------------------------------------


def test_start_checkout_records_pending_and_posts_the_right_payload(db):
    path, account_id = db
    fw = FakeFlutterwave()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test/", client=fw.client()
    )

    assert out.link == fw.link
    assert out.amount_xof == 12_000
    assert out.tx_ref.startswith(f"kodji-{account_id}-month-")

    req = fw.requests[0]
    assert req.method == "POST" and req.url.path == "/v3/payments"
    assert req.headers["authorization"] == "Bearer FLWSECK_TEST-secret"
    body = json.loads(req.content)
    assert body["tx_ref"] == out.tx_ref
    assert body["amount"] == "12000"  # zero-decimal, string, no x100
    assert body["currency"] == "XOF"
    assert body["redirect_url"] == "https://kodji.test/billing/return"
    assert body["customer"] == {"email": EMAIL}
    assert body["meta"] == {"account_id": str(account_id), "period": "month"}
    assert "payment_options" not in body  # dashboard decides the methods

    with connect(path) as conn:
        row = payments_repo.get_by_tx_ref(conn, out.tx_ref)
    assert row["status"] == "pending"
    assert row["amount_xof"] == 12_000
    assert row["customer_email"] == EMAIL


def test_start_checkout_marks_failed_when_provider_refuses(db):
    path, account_id = db
    fw = FakeFlutterwave()
    fw.fail_create = True
    with pytest.raises(billing.BillingError):
        start_checkout(account_id, EMAIL, "year", base_url="https://kodji.test", client=fw.client())
    with connect(path) as conn:
        rows = payments_repo.list_for_account(conn, account_id)
    assert rows[0]["status"] == "failed"
    assert rows[0]["note"] == "checkout_create_failed"


def test_start_checkout_without_keys_is_a_billing_error(db, monkeypatch):
    _, account_id = db
    monkeypatch.setenv("FLW_SECRET_KEY", "")
    reset_settings_cache()
    with pytest.raises(billing.BillingError):
        start_checkout(account_id, EMAIL, "month", base_url="https://kodji.test")


# --- confirm ----------------------------------------------------------------


def test_confirm_verifies_by_id_and_activates_for_one_month(db):
    path, account_id = db
    fw = FakeFlutterwave()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )
    fw.verified = _paid_payload(out.tx_ref, 12_000)
    now = at("2026-09-11T10:05:00")

    res = confirm(out.tx_ref, transaction_id="987654", client=fw.client(), now=now)

    assert res.outcome == "activated"
    assert res.period_end_utc == "2026-10-11T10:05:00Z"
    assert fw.requests[-1].url.path == "/v3/transactions/987654/verify"
    with connect(path) as conn:
        assert accounts_repo.plan_for(conn, account_id) == "paid"
        sub = accounts_repo.get_subscription(conn, account_id)
        row = payments_repo.get_by_tx_ref(conn, out.tx_ref)
    assert sub["provider"] == "flutterwave"
    assert sub["provider_ref"] == out.tx_ref
    assert sub["status"] == "active"
    assert row["status"] == "successful"
    assert row["provider_tx_id"] == "987654"
    assert row["payment_type"] == "mobilemoneyfranco"
    assert row["period_start_utc"] == "2026-09-11T10:05:00Z"
    assert row["period_end_utc"] == "2026-10-11T10:05:00Z"
    raw = json.loads(row["raw_json"])
    assert raw["flw_ref"] == "FLW-MOCK-1" and "customer" not in raw


def test_confirm_without_transaction_id_verifies_by_reference(db):
    _, account_id = db
    fw = FakeFlutterwave()
    out = start_checkout(
        account_id, EMAIL, "year", base_url="https://kodji.test", client=fw.client()
    )
    fw.verified = _paid_payload(out.tx_ref, 120_000)
    res = confirm(out.tx_ref, client=fw.client(), now=at("2026-09-11T10:05:00"))
    assert res.outcome == "activated"
    assert res.period_end_utc == "2027-09-11T10:05:00Z"
    last = fw.requests[-1]
    assert last.url.path == "/v3/transactions/verify_by_reference"
    assert last.url.params["tx_ref"] == out.tx_ref


def test_second_payment_extends_from_the_current_period_end(db):
    path, account_id = db
    fw = FakeFlutterwave()
    first = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )
    fw.verified = _paid_payload(first.tx_ref, 12_000)
    confirm(first.tx_ref, client=fw.client(), now=at("2026-09-11T10:00:00"))

    second = start_checkout(
        account_id, EMAIL, "year", base_url="https://kodji.test", client=fw.client()
    )
    fw.verified = _paid_payload(second.tx_ref, 120_000, id=111)
    res = confirm(second.tx_ref, client=fw.client(), now=at("2026-09-20T10:00:00"))

    # Paid 9 days early: the year starts where the month ends.
    assert res.period_end_utc == "2027-10-11T10:00:00Z"
    with connect(path) as conn:
        row = payments_repo.get_by_tx_ref(conn, second.tx_ref)
    assert row["period_start_utc"] == "2026-10-11T10:00:00Z"


def test_payment_after_a_lapse_starts_from_now(db):
    _, account_id = db
    fw = FakeFlutterwave()
    first = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )
    fw.verified = _paid_payload(first.tx_ref, 12_000)
    confirm(first.tx_ref, client=fw.client(), now=at("2026-01-01T00:00:00"))

    second = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )
    fw.verified = _paid_payload(second.tx_ref, 12_000, id=222)
    res = confirm(second.tx_ref, client=fw.client(), now=at("2026-09-11T10:00:00"))
    assert res.period_end_utc == "2026-10-11T10:00:00Z"


def test_confirm_is_idempotent(db):
    path, account_id = db
    fw = FakeFlutterwave()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )
    fw.verified = _paid_payload(out.tx_ref, 12_000)
    first = confirm(out.tx_ref, client=fw.client(), now=at("2026-09-11T10:00:00"))
    again = confirm(out.tx_ref, client=fw.client(), now=at("2026-09-11T10:30:00"))
    assert first.outcome == "activated"
    assert again.outcome == "already"
    assert again.period_end_utc == first.period_end_utc
    with connect(path) as conn:
        sub = accounts_repo.get_subscription(conn, account_id)
    assert sub["current_period_end_utc"] == first.period_end_utc  # not extended twice


@pytest.mark.parametrize(
    "over, reason",
    [
        ({"amount": 11_999}, "amount"),
        ({"currency": "NGN"}, "currency"),
        ({"tx_ref": "kodji-9-month-somebodyelse"}, "tx_ref"),
    ],
)
def test_confirm_refuses_a_mismatch(db, over, reason):
    path, account_id = db
    fw = FakeFlutterwave()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )
    fw.verified = _paid_payload(out.tx_ref, 12_000, **over)
    res = confirm(out.tx_ref, client=fw.client())
    assert res.outcome == "mismatch"
    assert res.note == reason
    with connect(path) as conn:
        assert accounts_repo.plan_for(conn, account_id) == "free"
        assert payments_repo.get_by_tx_ref(conn, out.tx_ref)["status"] == "failed"


def test_confirm_overpayment_is_accepted(db):
    _, account_id = db
    fw = FakeFlutterwave()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )
    fw.verified = _paid_payload(out.tx_ref, 12_500)
    assert confirm(out.tx_ref, client=fw.client()).outcome == "activated"


def test_confirm_pending_and_failed_statuses(db):
    path, account_id = db
    fw = FakeFlutterwave()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )
    fw.verified = _paid_payload(out.tx_ref, 12_000, status="pending")
    assert confirm(out.tx_ref, client=fw.client()).outcome == "pending"
    with connect(path) as conn:
        assert payments_repo.get_by_tx_ref(conn, out.tx_ref)["status"] == "pending"

    fw.verified = _paid_payload(out.tx_ref, 12_000, status="failed")
    res = confirm(out.tx_ref, client=fw.client())
    assert res.outcome == "failed"
    with connect(path) as conn:
        assert payments_repo.get_by_tx_ref(conn, out.tx_ref)["status"] == "failed"
        assert accounts_repo.plan_for(conn, account_id) == "free"


def test_confirm_unknown_reference_and_provider_outage(db):
    _, account_id = db
    fw = FakeFlutterwave()
    assert confirm("kodji-1-month-nope", client=fw.client()).outcome == "unknown"

    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    broken = FlutterwaveClient(
        "k", base_url=API, client=httpx.Client(transport=httpx.MockTransport(down))
    )
    res = confirm(out.tx_ref, client=broken)
    assert res.outcome == "unknown"
    assert "transport" in res.note


# --- webhook ----------------------------------------------------------------


def _event(tx_ref: str, **over) -> bytes:
    data = {
        "id": 987654,
        "tx_ref": tx_ref,
        "flw_ref": "FLW-1",
        "amount": 12000,
        "currency": "XOF",
        "status": "successful",
        "payment_type": "card",
        "customer": {"email": EMAIL},
    }
    data.update(over)
    return json.dumps({"event": "charge.completed", "data": data}).encode()


def test_webhook_rejects_bad_or_missing_hash(db):
    fw = FakeFlutterwave()
    assert (
        billing.handle_webhook(None, _event("kodji-1-month-x"), client=fw.client()).accepted
        is False
    )
    assert (
        billing.handle_webhook("wrong", _event("kodji-1-month-x"), client=fw.client()).accepted
        is False
    )
    assert fw.requests == []


def test_webhook_rejects_everything_when_no_hash_is_configured(db, monkeypatch):
    monkeypatch.setenv("FLW_WEBHOOK_HASH", "")
    reset_settings_cache()
    fw = FakeFlutterwave()
    assert (
        billing.handle_webhook("", _event("kodji-1-month-x"), client=fw.client()).accepted is False
    )


def test_webhook_reverifies_rather_than_trusting_the_body(db):
    path, account_id = db
    fw = FakeFlutterwave()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )
    # The body claims success, but the provider says the charge failed.
    fw.verified = _paid_payload(out.tx_ref, 12_000, status="failed")
    res = billing.handle_webhook("hash-123", _event(out.tx_ref), client=fw.client())
    assert res.accepted and res.action == "verified"
    assert res.confirmation.outcome == "failed"
    with connect(path) as conn:
        assert accounts_repo.plan_for(conn, account_id) == "free"

    # Now the provider agrees.
    out2 = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )
    fw.verified = _paid_payload(out2.tx_ref, 12_000)
    res = billing.handle_webhook("hash-123", _event(out2.tx_ref), client=fw.client())
    assert res.confirmation.outcome == "activated"
    assert fw.requests[-1].url.path == "/v3/transactions/987654/verify"


def test_webhook_ignores_other_events_and_foreign_refs(db):
    fw = FakeFlutterwave()
    other = json.dumps({"event": "transfer.completed", "data": {"id": 1}}).encode()
    assert billing.handle_webhook("hash-123", other, client=fw.client()).action == "ignored"
    assert (
        billing.handle_webhook("hash-123", _event("Links-616626414629"), client=fw.client()).action
        == "ignored"
    )
    assert (
        billing.handle_webhook("hash-123", b"not json", client=fw.client()).action == "bad_payload"
    )
    assert fw.requests == []


# --- plan_for + lifecycle ---------------------------------------------------


def test_plan_for_reads_a_past_period_end_as_free(db):
    path, account_id = db
    with connect(path) as conn:
        accounts_repo.set_plan(
            conn, account_id, "paid", status="active", current_period_end_utc="2026-01-01T00:00:00Z"
        )
        assert accounts_repo.plan_for(conn, account_id) == "free"
        accounts_repo.set_plan(
            conn,
            account_id,
            "paid",
            status="active",
            current_period_end_utc=utc_iso(datetime.now(UTC) + timedelta(days=1)),
        )
        assert accounts_repo.plan_for(conn, account_id) == "paid"
        # NULL end = no expiry (the operator's account from 0019).
        accounts_repo.set_plan(
            conn, account_id, "paid", status="active", current_period_end_utc=None
        )
        assert accounts_repo.plan_for(conn, account_id) == "paid"


@pytest.fixture
def outbox(monkeypatch):
    mailer = ConsoleMailer()
    monkeypatch.setattr("kodji.services.billing.get_mailer", lambda: mailer)
    return mailer


def test_expire_lapsed_stamps_and_mails_once(db, outbox):
    path, account_id = db
    with connect(path) as conn:
        accounts_repo.set_plan(
            conn, account_id, "paid", status="active", current_period_end_utc="2026-09-10T00:00:00Z"
        )
    now = at("2026-09-11T00:50:00")
    assert billing.expire_lapsed(now) == {"expired": 1, "mailed": 1}
    assert outbox.sent[0].to == EMAIL
    assert "pris fin" in outbox.sent[0].text
    assert "https://kodji.test/pricing" in outbox.sent[0].text
    with connect(path) as conn:
        sub = accounts_repo.get_subscription(conn, account_id)
        assert sub["status"] == "expired"
        assert sub["plan"] == "paid"  # history intact; plan_for says free
        assert accounts_repo.plan_for(conn, account_id) == "free"
    # Second pass: nothing left to expire, no second mail.
    assert billing.expire_lapsed(now + timedelta(hours=1)) == {"expired": 0, "mailed": 0}
    assert len(outbox.sent) == 1


def test_remind_expiring_sends_each_stage_once(db, outbox):
    path, account_id = db
    with connect(path) as conn:
        accounts_repo.set_plan(
            conn, account_id, "paid", status="active", current_period_end_utc="2026-09-18T07:00:00Z"
        )
    # 8 days out: nothing.
    assert billing.remind_expiring(at("2026-09-10T08:00:00")) == {"sent": 0}
    # 7 days out: the first reminder; the next day it is not repeated.
    assert billing.remind_expiring(at("2026-09-11T08:00:00")) == {"sent": 1}
    assert billing.remind_expiring(at("2026-09-12T08:00:00")) == {"sent": 0}
    assert "7 jour" in outbox.sent[0].subject
    assert "2026-09-18" in outbox.sent[0].text
    # 1 day out: the second reminder, once.
    assert billing.remind_expiring(at("2026-09-17T12:00:00")) == {"sent": 1}
    assert billing.remind_expiring(at("2026-09-17T20:00:00")) == {"sent": 0}
    assert "1 jour" in outbox.sent[1].subject
    # Past the end: nothing more from the reminder job.
    assert billing.remind_expiring(at("2026-09-19T08:00:00")) == {"sent": 0}
    assert len(outbox.sent) == 2


def test_status_for_reports_plan_and_history(db):
    _, account_id = db
    fw = FakeFlutterwave()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )
    fw.verified = _paid_payload(out.tx_ref, 12_000)
    confirm(out.tx_ref, client=fw.client(), now=at("2026-09-11T10:00:00"))
    status = billing.status_for(account_id)
    assert status.plan == "paid"
    assert status.period_end_day == "2026-10-11"
    assert [p["tx_ref"] for p in status.payments] == [out.tx_ref]


def test_abandon_closes_only_a_pending_row(db):
    path, account_id = db
    fw = FakeFlutterwave()
    out = start_checkout(
        account_id, EMAIL, "month", base_url="https://kodji.test", client=fw.client()
    )
    res = billing.abandon(out.tx_ref, reason="cancelled")
    assert res.outcome == "failed" and res.note == "cancelled"
    with connect(path) as conn:
        assert payments_repo.get_by_tx_ref(conn, out.tx_ref)["status"] == "failed"
    # The webhook can still land a charge that went through after all.
    fw.verified = _paid_payload(out.tx_ref, 12_000)
    assert confirm(out.tx_ref, client=fw.client()).outcome == "activated"
    # ...after which abandon is a no-op.
    assert billing.abandon(out.tx_ref).outcome == "already"
    assert billing.abandon("kodji-1-month-nope").outcome == "unknown"
