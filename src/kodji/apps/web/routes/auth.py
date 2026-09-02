"""Sign-in routes.

The flow's shape is in `services/auth.py`; this module is the HTTP skin
on it. Two things here are load-bearing rather than incidental:

**`GET /login/t/{token}` does not sign anyone in.** It renders a page
with a button, and the POST behind that button consumes the challenge.
Mail scanners and link previewers fetch every URL in a message; if the
GET consumed the token they would burn it before the user ever clicked,
and the user would see "this link has expired" on a link they just
received.

**The origin for the emailed link comes from settings in production.**
`request.base_url` is derived from the Host header, which behind
Cloudflare and Caddy is whatever the last hop claimed. A spoofed Host
would mint a link pointing at an attacker's domain — carrying a live
credential. `PUBLIC_BASE_URL` closes that; the request-derived fallback
is for local dev, where there is no proxy.

**Every POST here checks its `Origin`.** The threat is login CSRF: an
attacker's page auto-submits *their* magic link into a victim's browser,
the victim ends up signed into the attacker's account, and anything they
then save — a watchlist, an alert rule — lands somewhere the attacker can
read. `SameSite=lax` doesn't help, because the request that does the
damage carries no cookie of ours at all.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from kodji.apps.web._common import base_ctx, resolve_locale, templates
from kodji.config import settings
from kodji.logging import get
from kodji.services import accounts as accounts_svc
from kodji.services import auth as auth_svc
from kodji.services.auth import SESSION_COOKIE

log = get(__name__)

router = APIRouter()


def _base_url(request: Request) -> str:
    return settings.public_base_url.rstrip("/") or str(request.base_url).rstrip("/")


def _cross_origin(request: Request) -> bool:
    """True when this POST came from somewhere that isn't us.

    Browsers have sent `Origin` on cross-site POSTs for years, so a header
    that is present and doesn't match ours is the signal. An *absent*
    Origin is allowed through: that's curl, the test client, and older
    same-origin form posts — none of which an attacker controls a
    victim's browser into producing.
    """
    origin = request.headers.get("origin")
    if not origin:
        return False
    return urlparse(origin).netloc != urlparse(_base_url(request)).netloc


def _refuse_cross_origin(request: Request) -> Response | None:
    if not _cross_origin(request):
        return None
    log.warning("auth: rejected cross-origin POST from %s to %s",
                request.headers.get("origin"), request.url.path)
    return PlainTextResponse("cross-origin request refused", status_code=403)


def _sign_in(grant: auth_svc.Grant) -> RedirectResponse:
    """303 to the app with the session cookie set.

    `httponly` because nothing in the UI reads this from JS and a stored
    XSS should not be able to exfiltrate it — the opposite call from the
    locale cookie next door, which is a preference, not a credential.
    `samesite=lax` still lets the cookie ride the top-level navigation
    from the confirm page.
    """
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        grant.token,
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if accounts_svc.identity_for(request) is not None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", base_ctx(request))


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(...)):
    if (refused := _refuse_cross_origin(request)) is not None:
        return refused
    locale = resolve_locale(request)
    result = auth_svc.request_login(
        email, locale=locale, base_url=_base_url(request)
    )

    if result.note == "invalid_email":
        return templates.TemplateResponse(
            request,
            "login.html",
            {**base_ctx(request), "error": "invalid_email", "email": email},
            status_code=400,
        )

    # A rate-limited address gets the same page as a successful send. The
    # user genuinely does have a live link in their mailbox from a moment
    # ago, and saying "too many requests" would confirm to a stranger
    # that someone has been asking for links to this address.
    if result.note == "send_failed":
        return templates.TemplateResponse(
            request,
            "login.html",
            {**base_ctx(request), "error": "send_failed", "email": email},
            status_code=502,
        )

    return templates.TemplateResponse(
        request,
        "login_sent.html",
        {**base_ctx(request), "email": result.email or email},
    )


@router.get("/login/t/{token}", response_class=HTMLResponse)
def login_confirm(request: Request, token: str):
    """Render the confirm button. Deliberately read-only — see module docstring."""
    email = auth_svc.peek_token(token)
    if email is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {**base_ctx(request), "error": "expired_link"},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "login_confirm.html",
        {**base_ctx(request), "email": email, "token": token},
    )


@router.post("/login/t/{token}")
def login_complete(request: Request, token: str):
    if (refused := _refuse_cross_origin(request)) is not None:
        return refused
    grant = auth_svc.complete_with_token(token)
    if grant is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {**base_ctx(request), "error": "expired_link"},
            status_code=400,
        )
    return _sign_in(grant)


@router.post("/login/code")
def login_with_code(request: Request, email: str = Form(...), code: str = Form(...)):
    if (refused := _refuse_cross_origin(request)) is not None:
        return refused
    grant = auth_svc.complete_with_code(email, code)
    if grant is None:
        # One message for a wrong code, an expired challenge and a burned
        # one alike: distinguishing them tells a guesser how close they are.
        return templates.TemplateResponse(
            request,
            "login_sent.html",
            {**base_ctx(request), "email": email, "error": "bad_code"},
            status_code=400,
        )
    return _sign_in(grant)


@router.post("/logout")
def logout(request: Request):
    """POST, not GET, so a prefetch or an <img> tag can't sign a user out."""
    if (refused := _refuse_cross_origin(request)) is not None:
        return refused
    auth_svc.logout(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
