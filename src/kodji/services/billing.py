"""Billing (PR-Z): paid plan periods bought through Flutterwave.

**Pay per period, not auto-renew.** Flutterwave's recurring "payment
plans" pin the checkout to *card* — a customer arriving with Orange Money,
Wave or MTN MoMo cannot subscribe through one, and in WAEMU that is most
customers. So every purchase here is a fixed period (one month or one
year) paid once through the hosted Standard checkout, which offers every
method enabled on the dashboard. Buying again *extends* the current
period rather than restarting it, and reminder mail goes out before the
period ends. Card auto-renew can be layered on later as a second path;
nothing here assumes it away.

**Never trust the redirect, never trust the webhook body.** Both carry a
`tx_ref` we minted and nothing more; the plan is activated only after
`GET /transactions/{id}/verify` (or `verify_by_reference`) says the
transaction is `successful`, in `XOF`, for at least the price of the
period, against that exact `tx_ref`. The webhook is authenticated by the
`verif-hash` header, then treated as a hint to run the same verification.

**Idempotent by construction.** `payments.tx_ref` is unique and a row
already `successful` is never activated twice, so the redirect and the
webhook can both arrive, in either order, any number of times.

**XOF is zero-decimal.** Prices are INTEGER francs end to end; the API
is sent `"12000"`, never `1200000`.
"""

from __future__ import annotations

import hmac
import json
import secrets
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

PROVIDER = "flutterwave"
CURRENCY = "XOF"
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


# --- provider client --------------------------------------------------------


class BillingError(RuntimeError):
    """The provider refused or could not be reached. The message is safe
    to log; it never contains the secret key."""


class FlutterwaveClient:
    """The three v3 calls this integration needs. Injectable so tests run
    against an `httpx.MockTransport`."""

    def __init__(
        self,
        secret_key: str,
        *,
        base_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = (base_url or settings.flw_api_base).rstrip("/")
        self._owns = client is None
        self._client = client or httpx.Client(timeout=settings.http_timeout_s)
        self._headers = {"Authorization": f"Bearer {secret_key}"}

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            resp = self._client.request(method, f"{self._base}{path}", headers=self._headers, **kw)
        except httpx.HTTPError as e:
            raise BillingError(f"transport: {type(e).__name__}") from e
        try:
            body = resp.json()
        except ValueError as e:
            raise BillingError(f"http {resp.status_code}: non-JSON body") from e
        if resp.status_code >= 400 or body.get("status") != "success":
            raise BillingError(f"http {resp.status_code}: {str(body.get('message', ''))[:200]}")
        return body

    def create_checkout(self, payload: dict) -> str:
        """POST /payments → the hosted checkout link."""
        body = self._call("POST", "/payments", json=payload)
        link = (body.get("data") or {}).get("link")
        if not link:
            raise BillingError("no checkout link in response")
        return str(link)

    def verify_by_id(self, transaction_id: str) -> dict:
        return self._call("GET", f"/transactions/{transaction_id}/verify").get("data") or {}

    def verify_by_ref(self, tx_ref: str) -> dict:
        return (
            self._call("GET", "/transactions/verify_by_reference", params={"tx_ref": tx_ref}).get(
                "data"
            )
            or {}
        )


def _client(client: FlutterwaveClient | None) -> tuple[FlutterwaveClient, bool]:
    if client is not None:
        return client, False
    if not settings.has_billing:
        raise BillingError("billing is not configured (FLW_SECRET_KEY / FLW_PUBLIC_KEY)")
    return FlutterwaveClient(settings.flw_secret_key), True


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


def new_tx_ref(account_id: int, period_key: str) -> str:
    return f"kodji-{account_id}-{period_key}-{secrets.token_hex(6)}"


def start_checkout(
    account_id: int,
    email: str,
    period_key: str,
    *,
    base_url: str,
    client: FlutterwaveClient | None = None,
) -> Checkout:
    """Record a pending payment and get the hosted checkout link.

    The pending row is written *before* the provider call so a redirect
    for a `tx_ref` we have no record of can be refused outright.
    `payment_options` is deliberately not sent: the dashboard's enabled
    methods for XOF (cards, Orange Money, Wave, MTN MoMo) then all show.
    """
    period = PERIODS[period_key]
    tx_ref = new_tx_ref(account_id, period_key)
    amount = period.price_xof
    with connect(_db_path()) as conn:
        payments_repo.create_pending(
            conn,
            account_id=account_id,
            tx_ref=tx_ref,
            period=period.key,
            amount_xof=amount,
            customer_email=email,
        )
    payload = {
        "tx_ref": tx_ref,
        "amount": str(amount),
        "currency": CURRENCY,
        "redirect_url": f"{base_url.rstrip('/')}/billing/return",
        "customer": {"email": email},
        "customizations": {"title": "Kodji Terminal"},
        "meta": {"account_id": str(account_id), "period": period.key},
    }
    fw, owns = _client(client)
    try:
        link = fw.create_checkout(payload)
    except BillingError:
        with connect(_db_path()) as conn:
            payments_repo.mark_failed(conn, tx_ref, note="checkout_create_failed")
        raise
    finally:
        if owns:
            fw.close()
    log.info(
        "billing: checkout started account=%s period=%s tx_ref=%s", account_id, period.key, tx_ref
    )
    return Checkout(tx_ref=tx_ref, link=link, amount_xof=amount, period=period)


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


def _activate(conn, payment, data: dict, now: datetime) -> str:
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
        provider=PROVIDER,
        provider_ref=payment["tx_ref"],
        status="active",
        current_period_end_utc=utc_iso(end),
    )
    payments_repo.mark_successful(
        conn,
        payment["tx_ref"],
        provider_tx_id=str(data.get("id") or ""),
        provider_ref=str(data.get("flw_ref") or ""),
        payment_type=str(data.get("payment_type") or ""),
        paid_utc=utc_iso(now),
        period_start_utc=utc_iso(start),
        period_end_utc=utc_iso(end),
        raw_json=json.dumps(_safe_raw(data))[:4000],
    )
    return utc_iso(end)


def _safe_raw(data: dict) -> dict:
    """What we keep of the verified payload: enough for support, without
    card details or the customer's phone number."""
    keys = (
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
    )
    return {k: data.get(k) for k in keys if k in data}


def _matches(payment, data: dict) -> str | None:
    """None when the verified transaction pays for this payment row, else
    the reason it does not."""
    if str(data.get("tx_ref") or "") != payment["tx_ref"]:
        return "tx_ref"
    if str(data.get("currency") or "").upper() != CURRENCY:
        return "currency"
    try:
        paid = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        return "amount"
    if paid + 1e-9 < float(payment["amount_xof"]):
        return "amount"
    return None


def confirm(
    tx_ref: str,
    *,
    transaction_id: str | None = None,
    client: FlutterwaveClient | None = None,
    now: datetime | None = None,
) -> Confirmation:
    """Verify `tx_ref` with the provider and activate the plan if it paid.

    Safe to call from the browser redirect and the webhook alike, and
    repeatedly: only the first successful verification changes anything.
    """
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
        fw, owns = _client(client)
    except BillingError as e:
        return Confirmation("unknown", tx_ref, account_id, note=str(e))
    try:
        data = fw.verify_by_id(transaction_id) if transaction_id else fw.verify_by_ref(tx_ref)
    except BillingError as e:
        log.warning("billing: verify failed for %s: %s", tx_ref, e)
        return Confirmation("unknown", tx_ref, account_id, note=str(e))
    finally:
        if owns:
            fw.close()

    status = str(data.get("status") or "").lower()
    if status != "successful":
        if status in ("failed", "cancelled", "canceled", "abandoned"):
            with connect(_db_path()) as conn:
                payments_repo.mark_failed(conn, tx_ref, note=status)
            return Confirmation("failed", tx_ref, account_id, note=status)
        return Confirmation("pending", tx_ref, account_id, note=status or "no_status")

    reason = _matches(payment, data)
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
        end = _activate(conn, payment, data, now)
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
    verif_hash: str | None,
    body: bytes,
    *,
    client: FlutterwaveClient | None = None,
) -> WebhookResult:
    """Authenticate with the dashboard's secret hash, then re-verify.

    The body is never trusted for amounts or status — it only tells us
    which `tx_ref` to go and verify. An unconfigured hash rejects every
    call: a webhook that cannot be authenticated must not exist.
    """
    expected = settings.flw_webhook_hash
    if not expected or not verif_hash or not hmac.compare_digest(verif_hash, expected):
        log.warning("billing: webhook rejected (bad or missing verif-hash)")
        return WebhookResult(False, "bad_signature")
    try:
        event = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return WebhookResult(True, "bad_payload")
    data = event.get("data") if isinstance(event, dict) else None
    if event.get("event") != "charge.completed" or not isinstance(data, dict):
        return WebhookResult(True, "ignored")
    tx_ref = str(data.get("tx_ref") or "")
    tx_id = str(data.get("id") or "") or None
    if not tx_ref.startswith("kodji-"):
        return WebhookResult(True, "ignored")
    result = confirm(tx_ref, transaction_id=tx_id, client=client)
    return WebhookResult(True, "verified", result)


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
