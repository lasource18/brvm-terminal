"""Checkout, return and webhook routes (PR-Z).

Three doors, three trust levels:

- `POST /billing/checkout` is a signed-in user clicking a button. Origin
  is checked like every other POST, and the account and email come from
  the session, never the form.
- `GET /billing/return` is the customer coming back from the hosted
  checkout page. Its query string is decoration: the service verifies
  with the provider before anything changes.
- `POST /billing/webhook` is Flutterwave's server. No session, no Origin;
  it is authenticated by the `verif-hash` header and, again, re-verified.
  It must answer 200 fast or Flutterwave retries.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from kodji.apps.web._common import base_ctx, templates
from kodji.apps.web.routes.auth import _base_url, _refuse_cross_origin
from kodji.config import settings
from kodji.logging import get
from kodji.services import accounts as accounts_svc
from kodji.services import billing as billing_svc

log = get(__name__)

router = APIRouter()


def _closed(request: Request, status: int = 503) -> Response:
    return templates.TemplateResponse(
        request, "billing_closed.html", base_ctx(request), status_code=status
    )


@router.post("/billing/checkout")
def checkout(request: Request, period: str = Form(...)):
    if (refused := _refuse_cross_origin(request)) is not None:
        return refused
    identity = accounts_svc.identity_for(request)
    if identity is None:
        # The email on the receipt and the account being extended both
        # come from the session; without one there is nothing to sell to.
        return RedirectResponse(url="/login", status_code=303)
    if period not in billing_svc.PERIODS:
        return PlainTextResponse("unknown period", status_code=400)
    if not settings.has_billing:
        return _closed(request)
    try:
        session = billing_svc.start_checkout(
            identity.account_id, identity.email, period, base_url=_base_url(request)
        )
    except billing_svc.BillingError as e:
        log.error("billing: checkout failed for account %s: %s", identity.account_id, e)
        return _closed(request, status=502)
    return RedirectResponse(url=session.link, status_code=303)


@router.get("/billing/return", response_class=HTMLResponse)
def checkout_return(
    request: Request,
    status: str = "",
    tx_ref: str = "",
    transaction_id: str = "",
):
    if not tx_ref:
        return RedirectResponse(url="/pricing", status_code=303)
    # A cancelled checkout comes back with no transaction id and nothing
    # for the provider to verify; asking it would only say "not found".
    # The redirect is untrusted, so this can only close a *pending* row.
    if status.lower() in ("cancelled", "canceled", "failed") and not transaction_id:
        result = billing_svc.abandon(tx_ref, reason=status.lower())
    else:
        # The customer is watching this page: ask a few times over ~6 s
        # before saying "in progress", since a test-mode mobile money
        # charge is recorded a few seconds after the redirect.
        result = billing_svc.confirm(
            tx_ref, transaction_id=transaction_id or None, attempts=4, delay_s=2.0
        )
    ctx = {
        **base_ctx(request),
        "outcome": result.outcome,
        "period_end_day": (result.period_end_utc or "")[:10] or None,
        "provider_status": status,
        # The pending page re-checks itself by reloading this URL.
        "recheck_url": str(request.url),
    }
    code = 200 if result.outcome in ("activated", "already", "pending") else 402
    return templates.TemplateResponse(request, "billing_return.html", ctx, status_code=code)


@router.post("/billing/webhook")
async def webhook(request: Request):
    body = await request.body()
    result = billing_svc.handle_webhook(request.headers.get("verif-hash"), body)
    if not result.accepted:
        return PlainTextResponse("unauthorized", status_code=401)
    outcome = result.confirmation.outcome if result.confirmation else "-"
    log.info("billing: webhook %s (%s)", result.action, outcome)
    return PlainTextResponse("ok")


@router.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    """The account's plan, period end and payment history."""
    identity = accounts_svc.identity_for(request)
    if identity is None:
        return RedirectResponse(url="/login", status_code=303)
    status = billing_svc.status_for(identity.account_id)
    return templates.TemplateResponse(
        request,
        "billing.html",
        {**base_ctx(request), "billing": status, "fmt_xof": billing_svc.fmt_xof},
    )
