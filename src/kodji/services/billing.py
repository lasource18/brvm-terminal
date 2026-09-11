"""Billing (PR-Z): paid plan periods bought through a hosted checkout.

**Pay per period, not auto-renew.** Recurring "plans" at the gateways pin
the checkout to *card* — a customer arriving with Orange Money, Wave or
MTN MoMo cannot subscribe through one, and in WAEMU that is most
customers. So every purchase here is a fixed period (one month or one
year) paid once through the provider's hosted page. Buying again
*extends* the current period rather than restarting it, and reminder
mail goes out before the period ends. Card auto-renew can be layered on
later as a second path; nothing here assumes it away.

**Two providers, one setting.** `BILLING_PROVIDER` picks `flutterwave`
or `paystack`. Each adapter answers the same three questions — give me a
checkout link, what happened to this reference, is this webhook really
from you — and normalises the answer to one shape, so everything above
(activation, reminders, expiry, pages) is provider-blind. Switching in
production is an `.env` change.

**Never trust the redirect, never trust the webhook body.** Both carry a
`tx_ref` we minted and nothing more; the plan is activated only after the
provider's *verify* call says the transaction succeeded, in `XOF`, for at
least the price of the period, against that exact `tx_ref`. The webhook
is authenticated (Flutterwave: `verif-hash`; Paystack: HMAC-SHA512 in
`x-paystack-signature`), then treated as a hint to run that verification.

**Idempotent by construction.** `payments.tx_ref` is unique and a row
already `successful` is never activated twice, so the redirect and the
webhook can both arrive, in either order, any number of times.

**XOF is zero-decimal — but not to every gateway.** Prices are INTEGER
francs everywhere in this codebase. Flutterwave takes francs as-is;
Paystack expects hundredths (its checkout shows "XOF 120" for `12000`),
so that adapter multiplies by 100 on the way out and divides on the way
back. Nothing outside the adapters knows.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from dateutil.relativedelta import relativedelta

from kodji.clock import utc_iso, utcnow
from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.services.mailer import EmailMessage, get_mailer
from kodji.store import accounts as accounts_repo
from kodji.store import payments as payments_repo
from kodji.store.accounts import PAID_PLAN

log = get(__name__)

CURRENCY = "XOF"
PROVIDERS = ("flutterwave", "paystack")
# Days before the period ends at which a reminder goes out. Each stage
# is sent once per period end (see `billing_notices`).
REMINDER_STAGES: tuple[tuple[str, int], ...] = (("expiring_7d", 7), ("expiring_1d", 1))


# --- catalogue --------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    key: str
    months: int
    label_en: str
    label_fr: str

    @property
    def price_xof(self) -> int:
        return int(settings.price_year_xof if self.key == "year" else settings.price_month_xof)


PERIODS: dict[str, Period] = {
    "month": Period("month", 1, "1 month", "1 mois"),
    "year": Period("year", 12, "12 months", "12 mois"),
}


def fmt_xof(amount: int) -> str:
    """`12000` → `"12 000 XOF"`, the way prices are written locally."""
    return f"{int(amount):,}".replace(",", " ") + " XOF"


# --- providers --------------------------------------------------------------


class BillingError(RuntimeError):
    """The provider refused or could not be reached. The message is safe
    to log; it never contains the secret key."""


class NotFound(BillingError):
    """The provider has no transaction for that reference *yet*. In test
    mode a mobile money charge is recorded a few seconds after the
    customer is redirected, so this is usually "ask again", not "no"."""


@dataclass(frozen=True)
class Verified:
    """One verified transaction, in provider-neutral terms.

    `status` is `successful`, `failed` or `pending`; `amount_xof` is in
    whole francs whatever unit the gateway speaks; `raw` is the subset of
    the provider payload kept for support (no card or phone data).
    """

    status: str
    amount_xof: float
    currency: str
    tx_ref: str
    provider_tx_id: str
    provider_ref: str
    payment_type: str
    raw: dict


class _HttpProvider:
    """Shared plumbing: one `httpx.Client`, bearer auth, JSON errors."""

    name = ""

    def __init__(
        self,
        secret_key: str,
        *,
        base_url: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._secret = secret_key
        self._owns = client is None
        self._client = client or httpx.Client(timeout=settings.http_timeout_s)
        self._headers = {"Authorization": f"Bearer {secret_key}"}

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def _request(self, method: str, path: str, **kw) -> tuple[int, dict]:
        try:
            resp = self._client.request(method, f"{self._base}{path}", headers=self._headers, **kw)
        except httpx.HTTPError as e:
            raise BillingError(f"transport: {type(e).__name__}") from e
        try:
            body = resp.json()
        except ValueError as e:
            raise BillingError(f"http {resp.status_code}: non-JSON body") from e
        return resp.status_code, body if isinstance(body, dict) else {}

    @staticmethod
    def _pick(data: dict, keys: tuple[str, ...]) -> dict:
        return {k: data.get(k) for k in keys if k in data}


class FlutterwaveClient(_HttpProvider):
    """Flutterwave v3: hosted Standard checkout, verify, `verif-hash`."""

    name = "flutterwave"

    def __init__(
        self,
        secret_key: str,
        *,
        base_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(secret_key, base_url=base_url or settings.flw_api_base, client=client)

    def _call(self, method: str, path: str, **kw) -> dict:
        code, body = self._request(method, path, **kw)
        if code >= 400 or body.get("status") != "success":
            message = str(body.get("message", ""))[:200]
            low = message.lower()
            if code in (400, 404) and ("not found" in low or "was found" in low):
                raise NotFound(f"http {code}: {message}")
            raise BillingError(f"http {code}: {message}")
        return body

    def create_checkout(
        self, *, tx_ref: str, amount_xof: int, email: str, redirect_url: str, meta: dict
    ) -> str:
        payload = {
            "tx_ref": tx_ref,
            "amount": str(amount_xof),  # francs, as a string
            "currency": CURRENCY,
            "redirect_url": redirect_url,
            "customer": {"email": email},
            "customizations": {"title": "Kodji Terminal"},
            "meta": meta,
        }
        # Honoured only once "Enable Dashboard Payment Options" is
        # unchecked on the dashboard; otherwise a fresh sandbox shows card.
        if settings.flw_payment_options:
            payload["payment_options"] = settings.flw_payment_options
        body = self._call("POST", "/payments", json=payload)
        link = (body.get("data") or {}).get("link")
        if not link:
            raise BillingError("no checkout link in response")
        return str(link)

    def verify(self, tx_ref: str, transaction_id: str | None = None) -> Verified:
        if transaction_id:
            body = self._call("GET", f"/transactions/{transaction_id}/verify")
        else:
            body = self._call("GET", "/transactions/verify_by_reference", params={"tx_ref": tx_ref})
        data = body.get("data") or {}
        status = str(data.get("status") or "").lower()
        if status == "successful":
            norm = "successful"
        elif status in ("failed", "cancelled", "canceled", "abandoned"):
            norm = "failed"
        else:
            norm = "pending"
        return Verified(
            status=norm,
            amount_xof=_num(data.get("amount")),
            currency=str(data.get("currency") or "").upper(),
            tx_ref=str(data.get("tx_ref") or ""),
            provider_tx_id=str(data.get("id") or ""),
            provider_ref=str(data.get("flw_ref") or ""),
            payment_type=str(data.get("payment_type") or ""),
            raw=self._pick(
                data,
                (
                    "id",
                    "tx_ref",
                    "flw_ref",
                    "amount",
                    "currency",
                    "charged_amount",
                    "status",
                    "payment_type",
                    "created_at",
                    "processor_response",
                    "narration",
                ),
            ),
        )

    def webhook_authentic(self, headers: Mapping[str, str], body: bytes) -> bool:
        """The dashboard's secret hash comes back verbatim in `verif-hash`.
        No configured hash → nothing is authentic."""
        expected = settings.flw_webhook_hash
        got = _header(headers, "verif-hash")
        return bool(expected and got and hmac.compare_digest(got, expected))

    @staticmethod
    def webhook_reference(event: dict) -> tuple[str, str | None] | None:
        data = event.get("data")
        if event.get("event") != "charge.completed" or not isinstance(data, dict):
            return None
        return str(data.get("tx_ref") or ""), (str(data.get("id") or "") or None)


class PaystackClient(_HttpProvider):
    """Paystack: initialize → hosted page, verify by reference, HMAC webhook.

    Paystack bills XOF in hundredths: `amount` on the wire is francs x 100
    and comes back the same way. `channels` limits the hosted page to
    what `PAYSTACK_CHANNELS` names (cards and mobile money by default).
    """

    name = "paystack"
    SUBUNIT = 100

    def __init__(
        self,
        secret_key: str,
        *,
        base_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(secret_key, base_url=base_url or settings.paystack_api_base, client=client)

    def _call(self, method: str, path: str, **kw) -> dict:
        code, body = self._request(method, path, **kw)
        if code >= 400 or body.get("status") is not True:
            message = str(body.get("message", ""))[:200]
            if code == 404 or "not found" in message.lower():
                raise NotFound(f"http {code}: {message}")
            raise BillingError(f"http {code}: {message}")
        return body

    def create_checkout(
        self, *, tx_ref: str, amount_xof: int, email: str, redirect_url: str, meta: dict
    ) -> str:
        channels = [c.strip() for c in settings.paystack_channels.split(",") if c.strip()]
        payload = {
            "email": email,
            "amount": int(amount_xof) * self.SUBUNIT,
            "currency": CURRENCY,
            "reference": tx_ref,
            "callback_url": redirect_url,
            "metadata": meta,
        }
        if channels:
            payload["channels"] = channels
        body = self._call("POST", "/transaction/initialize", json=payload)
        link = (body.get("data") or {}).get("authorization_url")
        if not link:
            raise BillingError("no authorization_url in response")
        return str(link)

    def verify(self, tx_ref: str, transaction_id: str | None = None) -> Verified:
        # Paystack verifies by reference; the id is informational.
        del transaction_id
        body = self._call("GET", f"/transaction/verify/{tx_ref}")
        data = body.get("data") or {}
        status = str(data.get("status") or "").lower()
        if status == "success":
            norm = "successful"
        elif status in ("failed", "abandoned", "reversed"):
            norm = "failed"
        else:
            norm = "pending"  # ongoing, processing, queued, pending
        return Verified(
            status=norm,
            amount_xof=_num(data.get("amount")) / self.SUBUNIT,
            currency=str(data.get("currency") or "").upper(),
            tx_ref=str(data.get("reference") or ""),
            provider_tx_id=str(data.get("id") or ""),
            provider_ref=str(data.get("receipt_number") or data.get("reference") or ""),
            payment_type=str(data.get("channel") or ""),
            raw=self._pick(
                data,
                (
                    "id",
                    "reference",
                    "amount",
                    "currency",
                    "status",
                    "channel",
                    "paid_at",
                    "created_at",
                    "gateway_response",
                    "receipt_number",
                    "requested_amount",
                ),
            ),
        )

    def webhook_authentic(self, headers: Mapping[str, str], body: bytes) -> bool:
        """`x-paystack-signature` is HMAC-SHA512 of the raw body keyed with
        the secret key. Constant-time compare; no secret → nothing is
        authentic."""
        got = _header(headers, "x-paystack-signature")
        if not self._secret or not got:
            return False
        expected = hmac.new(self._secret.encode("utf-8"), body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(got.lower(), expected)

    @staticmethod
    def webhook_reference(event: dict) -> tuple[str, str | None] | None:
        data = event.get("data")
        if event.get("event") != "charge.success" or not isinstance(data, dict):
            return None
        return str(data.get("reference") or ""), (str(data.get("id") or "") or None)


Provider = FlutterwaveClient | PaystackClient


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return -1.0


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return None


def provider_name(explicit: str | None = None) -> str:
    name = (explicit or settings.billing_provider or "flutterwave").lower()
    if name not in PROVIDERS:
        raise BillingError(f"unknown BILLING_PROVIDER {name!r}")
    return name


def build_provider(name: str | None = None) -> Provider:
    """The configured (or named) provider, or `BillingError` when its keys
    are missing — checkout closed, webhook 401."""
    name = provider_name(name)
    if name == "paystack":
        if not settings.paystack_secret_key:
            raise BillingError("billing is not configured (PAYSTACK_SECRET_KEY)")
        return PaystackClient(settings.paystack_secret_key)
    if not settings.has_flutterwave:
        raise BillingError("billing is not configured (FLW_SECRET_KEY / FLW_PUBLIC_KEY)")
    return FlutterwaveClient(settings.flw_secret_key)


def _client(client: Provider | None, name: str | None = None) -> tuple[Provider, bool]:
    if client is not None:
        return client, False
    return build_provider(name), True


def _db_path() -> Path:
    return Path(settings.db_path)


def _parse(stamp: str) -> datetime:
    dt = datetime.fromisoformat(stamp)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --- checkout ---------------------------------------------------------------


@dataclass(frozen=True)
class Checkout:
    tx_ref: str
    link: str
    amount_xof: int
    period: Period
    provider: str


def new_tx_ref(account_id: int, period_key: str) -> str:
    return f"kodji-{account_id}-{period_key}-{secrets.token_hex(6)}"


def start_checkout(
    account_id: int,
    email: str,
    period_key: str,
    *,
    base_url: str,
    client: Provider | None = None,
) -> Checkout:
    """Record a pending payment and get the hosted checkout link.

    The pending row is written *before* the provider call so a redirect
    for a `tx_ref` we have no record of can be refused outright, and it
    remembers which provider was asked so the return and the webhook
    verify with the same one even if `BILLING_PROVIDER` changes later.
    """
    period = PERIODS[period_key]
    tx_ref = new_tx_ref(account_id, period_key)
    amount = period.price_xof
    fw, owns = _client(client)
    with connect(_db_path()) as conn:
        payments_repo.create_pending(
            conn,
            account_id=account_id,
            tx_ref=tx_ref,
            period=period.key,
            amount_xof=amount,
            customer_email=email,
            provider=fw.name,
        )
    try:
        link = fw.create_checkout(
            tx_ref=tx_ref,
            amount_xof=amount,
            email=email,
            redirect_url=f"{base_url.rstrip('/')}/billing/return",
            meta={"account_id": str(account_id), "period": period.key},
        )
    except BillingError:
        with connect(_db_path()) as conn:
            payments_repo.mark_failed(conn, tx_ref, note="checkout_create_failed")
        raise
    finally:
        if owns:
            fw.close()
    log.info(
        "billing: checkout started provider=%s account=%s period=%s tx_ref=%s",
        fw.name,
        account_id,
        period.key,
        tx_ref,
    )
    return Checkout(tx_ref=tx_ref, link=link, amount_xof=amount, period=period, provider=fw.name)


# --- confirmation -----------------------------------------------------------


@dataclass(frozen=True)
class Confirmation:
    """`outcome` is one of:
    - `activated`  — verified now; the plan was extended
    - `already`    — this tx_ref had been activated before (no change)
    - `pending`    — the provider has not settled it yet (mobile money)
    - `failed`     — the provider reports a failed/cancelled charge
    - `mismatch`   — settled, but amount/currency/reference do not match
    - `unknown`    — no such tx_ref, or the provider could not be reached
    """

    outcome: str
    tx_ref: str
    account_id: int | None = None
    period_end_utc: str | None = None
    note: str = ""


def _activate(conn, payment, v: Verified, now: datetime) -> str:
    """Extend the account's paid period by the payment's period. Returns
    the new period end. A period still running is extended from its end,
    so paying early never loses days."""
    period = PERIODS[payment["period"]]
    sub = accounts_repo.get_subscription(conn, int(payment["account_id"]))
    start = now
    if (
        sub is not None
        and sub["plan"] == PAID_PLAN
        and sub["current_period_end_utc"]
        and _parse(sub["current_period_end_utc"]) > now
    ):
        start = _parse(sub["current_period_end_utc"])
    end = start + relativedelta(months=period.months)
    accounts_repo.set_plan(
        conn,
        int(payment["account_id"]),
        PAID_PLAN,
        provider=str(payment["provider"] or ""),
        provider_ref=payment["tx_ref"],
        status="active",
        current_period_end_utc=utc_iso(end),
    )
    payments_repo.mark_successful(
        conn,
        payment["tx_ref"],
        provider_tx_id=v.provider_tx_id,
        provider_ref=v.provider_ref,
        payment_type=v.payment_type,
        paid_utc=utc_iso(now),
        period_start_utc=utc_iso(start),
        period_end_utc=utc_iso(end),
        raw_json=json.dumps(v.raw)[:4000],
    )
    return utc_iso(end)


def _matches(payment, v: Verified) -> str | None:
    """None when the verified transaction pays for this payment row, else
    the reason it does not."""
    if v.tx_ref != payment["tx_ref"]:
        return "tx_ref"
    if v.currency != CURRENCY:
        return "currency"
    if v.amount_xof + 1e-9 < float(payment["amount_xof"]):
        return "amount"
    return None


def confirm(
    tx_ref: str,
    *,
    transaction_id: str | None = None,
    client: Provider | None = None,
    now: datetime | None = None,
    attempts: int = 1,
    delay_s: float = 2.0,
) -> Confirmation:
    """Verify `tx_ref` with its provider and activate the plan if it paid.

    Safe to call from the browser redirect and the webhook alike, and
    repeatedly: only the first successful verification changes anything.

    `attempts > 1` re-asks while the answer is `pending` (including "not
    recorded yet"), sleeping `delay_s` between tries — for the browser
    return, where the customer is looking at the page and the sandbox
    settles a mobile money charge a few seconds after redirecting.
    """
    result = _confirm_once(tx_ref, transaction_id=transaction_id, client=client, now=now)
    for _ in range(max(0, attempts - 1)):
        if result.outcome != "pending":
            break
        time.sleep(delay_s)
        result = _confirm_once(tx_ref, transaction_id=transaction_id, client=client, now=now)
    return result


def _confirm_once(
    tx_ref: str,
    *,
    transaction_id: str | None,
    client: Provider | None,
    now: datetime | None,
) -> Confirmation:
    now = now or utcnow()
    with connect(_db_path()) as conn:
        payment = payments_repo.get_by_tx_ref(conn, tx_ref)
    if payment is None:
        log.warning("billing: confirm for unknown tx_ref %s", tx_ref)
        return Confirmation("unknown", tx_ref, note="unknown_tx_ref")
    account_id = int(payment["account_id"])
    if payment["status"] == "successful":
        return Confirmation("already", tx_ref, account_id, payment["period_end_utc"])

    try:
        # Verify with the provider that issued the checkout, not whatever
        # BILLING_PROVIDER says today.
        fw, owns = _client(client, payment["provider"] or None)
    except BillingError as e:
        return Confirmation("unknown", tx_ref, account_id, note=str(e))
    try:
        v = fw.verify(tx_ref, transaction_id)
    except NotFound as e:
        # Nothing recorded for this reference yet — the customer may still
        # be on the hosted page, or the charge is a few seconds behind the
        # redirect. Pending, not unknown: nothing has gone wrong.
        log.info("billing: %s not recorded at the provider yet (%s)", tx_ref, e)
        return Confirmation("pending", tx_ref, account_id, note="not_yet_recorded")
    except BillingError as e:
        log.warning("billing: verify failed for %s: %s", tx_ref, e)
        return Confirmation("unknown", tx_ref, account_id, note=str(e))
    finally:
        if owns:
            fw.close()

    if v.status == "failed":
        note = str(v.raw.get("status") or "failed")
        with connect(_db_path()) as conn:
            payments_repo.mark_failed(conn, tx_ref, note=note)
        return Confirmation("failed", tx_ref, account_id, note=note)
    if v.status != "successful":
        return Confirmation(
            "pending", tx_ref, account_id, note=str(v.raw.get("status") or "pending")
        )

    reason = _matches(payment, v)
    if reason is not None:
        log.error("billing: verified transaction does not match %s: %s", tx_ref, reason)
        with connect(_db_path()) as conn:
            payments_repo.mark_failed(conn, tx_ref, note=f"mismatch:{reason}")
        return Confirmation("mismatch", tx_ref, account_id, note=reason)

    with connect(_db_path()) as conn:
        # Re-read under the write connection: the webhook and the redirect
        # can race, and the second one must see the first one's result.
        payment = payments_repo.get_by_tx_ref(conn, tx_ref)
        if payment is None or payment["status"] == "successful":
            return Confirmation(
                "already", tx_ref, account_id, payment["period_end_utc"] if payment else None
            )
        end = _activate(conn, payment, v, now)
    log.info("billing: activated account=%s tx_ref=%s until=%s", account_id, tx_ref, end)
    return Confirmation("activated", tx_ref, account_id, end)


def abandon(tx_ref: str, *, reason: str = "cancelled") -> Confirmation:
    """The customer came back from the hosted page without paying.

    Only a *pending* row is touched, and only its status: nothing here
    trusts the redirect for anything that grants access. If a charge did
    go through after all, the webhook's `confirm` still activates it —
    `mark_successful` updates any non-successful row.
    """
    with connect(_db_path()) as conn:
        payment = payments_repo.get_by_tx_ref(conn, tx_ref)
        if payment is None:
            return Confirmation("unknown", tx_ref, note="unknown_tx_ref")
        if payment["status"] == "successful":
            return Confirmation(
                "already", tx_ref, int(payment["account_id"]), payment["period_end_utc"]
            )
        payments_repo.mark_failed(conn, tx_ref, note=reason)
    return Confirmation("failed", tx_ref, int(payment["account_id"]), note=reason)


# --- webhook ----------------------------------------------------------------


@dataclass(frozen=True)
class WebhookResult:
    accepted: bool  # False → answer 401; the caller must not act
    action: str  # verified | ignored | bad_signature | bad_payload
    confirmation: Confirmation | None = None


def handle_webhook(
    headers: Mapping[str, str],
    body: bytes,
    *,
    provider: str | None = None,
    client: Provider | None = None,
) -> WebhookResult:
    """Authenticate the delivery with the named provider, then re-verify.

    The body is never trusted for amounts or status — it only tells us
    which `tx_ref` to go and verify. A provider without its secret (or,
    for Flutterwave, without the dashboard hash) rejects every call: a
    webhook that cannot be authenticated must not exist.
    """
    try:
        fw, owns = _client(client, provider)
    except BillingError as e:
        log.warning("billing: webhook rejected (%s)", e)
        return WebhookResult(False, "bad_signature")
    try:
        if not fw.webhook_authentic(headers, body):
            log.warning("billing: %s webhook rejected (bad or missing signature)", fw.name)
            return WebhookResult(False, "bad_signature")
        try:
            event = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return WebhookResult(True, "bad_payload")
        ref = fw.webhook_reference(event) if isinstance(event, dict) else None
        if ref is None or not ref[0].startswith("kodji-"):
            return WebhookResult(True, "ignored")
        tx_ref, tx_id = ref
        result = confirm(tx_ref, transaction_id=tx_id, client=fw)
        return WebhookResult(True, "verified", result)
    finally:
        if owns:
            fw.close()


# --- lifecycle jobs ---------------------------------------------------------


def _owner_emails(conn, account_id: int) -> list[str]:
    return accounts_repo.member_emails(conn, account_id)


def _send(to: str, subject: str, text: str) -> bool:
    mailer = get_mailer()
    try:
        res = mailer.send(
            EmailMessage(to=to, subject=subject, text=text, html=f"<pre>{text}</pre>")
        )
    finally:
        mailer.close()
    if not res.ok:
        log.warning("billing: mail to %s failed: %s", to, res.note)
    return res.ok


def _pricing_url() -> str:
    return f"{settings.public_base_url.rstrip('/') or ''}/pricing"


def expire_lapsed(now: datetime | None = None) -> dict:
    """Flip paid subscriptions whose period has ended to `expired`.

    `plan_for` already reads a past `current_period_end_utc` as free, so
    access is cut on time whether or not this job has run; this makes the
    bookkeeping match and tells the customer once.
    """
    now = now or utcnow()
    stamp = utc_iso(now)
    expired = 0
    mailed = 0
    with connect(_db_path()) as conn:
        rows = accounts_repo.lapsed_paid_subscriptions(conn, stamp)
        for sub in rows:
            account_id = int(sub["account_id"])
            accounts_repo.mark_expired(conn, account_id)
            expired += 1
            if payments_repo.notice_sent(
                conn, account_id, sub["current_period_end_utc"], "expired"
            ):
                continue
            for email in _owner_emails(conn, account_id):
                if _send(
                    email,
                    "Votre abonnement Kodji a pris fin / Your Kodji plan has ended",
                    "Votre abonnement Kodji Terminal a pris fin. Les fonctions payantes "
                    "(graphiques, ratios, résumé quotidien, alertes) sont de nouveau en "
                    f"lecture gratuite. Renouveler : {_pricing_url()}\n\n"
                    "Your Kodji Terminal plan has ended. Paid features are back to the "
                    f"free tier. Renew: {_pricing_url()}",
                ):
                    mailed += 1
            payments_repo.record_notice(
                conn, account_id, sub["current_period_end_utc"], "expired", stamp
            )
    return {"expired": expired, "mailed": mailed}


def remind_expiring(now: datetime | None = None) -> dict:
    """Send the 7-day and 1-day reminders, once each per period end."""
    now = now or utcnow()
    stamp = utc_iso(now)
    sent = 0
    with connect(_db_path()) as conn:
        for kind, days in REMINDER_STAGES:
            horizon = utc_iso(now + timedelta(days=days))
            for sub in accounts_repo.paid_subscriptions_ending_before(conn, horizon, stamp):
                account_id = int(sub["account_id"])
                end = sub["current_period_end_utc"]
                if payments_repo.notice_sent(conn, account_id, end, kind):
                    continue
                end_day = end[:10]
                for email in _owner_emails(conn, account_id):
                    if _send(
                        email,
                        f"Votre abonnement Kodji expire dans {days} jour(s) / "
                        f"Your Kodji plan ends in {days} day(s)",
                        f"Votre abonnement Kodji Terminal prend fin le {end_day}. "
                        f"Prolongez-le en un paiement (mobile money ou carte) : {_pricing_url()}\n\n"
                        f"Your Kodji Terminal plan ends on {end_day}. Extend it with one "
                        f"payment (mobile money or card): {_pricing_url()}",
                    ):
                        sent += 1
                payments_repo.record_notice(conn, account_id, end, kind, stamp)
    return {"sent": sent}


# --- read side --------------------------------------------------------------


@dataclass(frozen=True)
class BillingStatus:
    plan: str
    status: str
    period_end_utc: str | None
    payments: list = field(default_factory=list)

    @property
    def period_end_day(self) -> str | None:
        return self.period_end_utc[:10] if self.period_end_utc else None


def status_for(account_id: int) -> BillingStatus:
    with connect(_db_path()) as conn:
        sub = accounts_repo.get_subscription(conn, account_id)
        payments = payments_repo.list_for_account(conn, account_id)
        plan = accounts_repo.plan_for(conn, account_id)
    return BillingStatus(
        plan=plan,
        status=str(sub["status"]) if sub else "none",
        period_end_utc=sub["current_period_end_utc"] if sub else None,
        payments=payments,
    )
