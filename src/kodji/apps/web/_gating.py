"""Plan enforcement for the web layer (PR-Y).

The product line, from `docs/kodji-plan.md` P4: **raw facts free, our own
work paid.** Quotes, the directory, the news listing and bond reference
data are scraped facts and stay free. Charts, ratios, peers, the brief,
alerts and the analyst view are things this app computed, and those are
the paid tier.

**Hiding a tab is not access control.** `tabs.visible_for` drops paid
tabs off the tabbar, but `/s/SNTS/yield` is still reachable by typing it,
and so are the HTMX fragments behind it and the JSON API under `/api`.
Three route families, three chances to forget. This module is the one
place that decides, and `tests/test_gating.py` walks every paid tab
across all three asserting a free caller is refused — that test is what
stops a regression the day a tab is added and the dependency isn't.

**Why a helper returning `Response | None` rather than a `Depends`.**
The refusal is not one shape: a full-page request wants an upgrade page,
an HTMX swap wants a fragment that lands inside the target div, and the
API wants JSON. A dependency can only raise, and an exception handler
would have to reconstruct which of the three it was from headers it
doesn't otherwise care about. Mirrors `_refuse_cross_origin` in
`routes/auth.py`, which solves the same problem for CSRF.

**Anonymous readers are free-tier, not blocked.** `current_account_id`
resolves a request with no session to the default account, whose
subscription reads `free`. So gating works without `AUTH_REQUIRED` being
on: a visitor sees the free product and an upgrade prompt, rather than a
login wall in front of public market data.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from kodji.apps.web._common import base_ctx, templates
from kodji.services import accounts as accounts_svc
from kodji.store.accounts import PAID_PLAN

# 402 is the honest code: the request is well-formed and the resource
# exists, payment is what is missing. It is unusual enough on the wire
# that a proxy will pass it through untouched, unlike 403.
PAYMENT_REQUIRED = 402


def plan_for_request(request: Request) -> str:
    """The plan to enforce for this request.

    Never raises: an unauthenticated caller under `AUTH_REQUIRED` would
    otherwise blow up inside a gating check, and "we could not identify
    you" resolves to the free tier here — the safe direction.
    """
    try:
        account_id = accounts_svc.current_account_id(request)
    except accounts_svc.NotAuthenticated:
        return "free"
    return accounts_svc.plan_for(account_id)


def is_paid(request: Request) -> bool:
    return plan_for_request(request) == PAID_PLAN


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def upgrade_response(request: Request, *, feature: str) -> Response:
    """The 402 a refused caller gets, shaped to how they asked.

    `feature` is a catalog source string naming what was blocked, so the
    page can say which thing needs a plan rather than showing a generic
    wall.
    """
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"error": "payment_required", "feature": feature},
            status_code=PAYMENT_REQUIRED,
        )

    ctx = {**base_ctx(request), "feature": feature}
    template = "_frag/upgrade.html" if _is_htmx(request) else "upgrade.html"
    return templates.TemplateResponse(
        request, template, ctx, status_code=PAYMENT_REQUIRED
    )


def refuse_if_unpaid(request: Request, *, feature: str) -> Response | None:
    """None when the caller may proceed, else the 402 to return.

    Call it as the first statement of any route serving paid work:

        if (refused := refuse_if_unpaid(request, feature="Chart")) is not None:
            return refused
    """
    if is_paid(request):
        return None
    return upgrade_response(request, feature=feature)
