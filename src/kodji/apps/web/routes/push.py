"""PWA shell and Web Push endpoints (PR-AA).

Three root-level files a browser expects at fixed paths, and the JSON
API the alerts page's "enable on this device" button talks to.

* `/manifest.webmanifest` — the installable-app descriptor.
* `/sw.js` — the service worker. Served from the root, not `/static/`,
  because a worker's scope cannot exceed its own path and it must
  control every page. The `__STATIC_V__` placeholder is replaced with
  the CSS/JS digest so the worker's precache list names the URLs the
  pages actually load, and a deploy that changes either yields a new
  worker byte-for-byte, which is what makes browsers install it.
* `/offline` — the page the worker shows when a navigation has no
  network. Precached at install.

The subscribe API needs a session (a device is tied to a user) and the
paid plan (alerts are paid; a free account has nothing to receive).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from kodji.apps.web._common import STATIC_DIR, STATIC_VERSION, base_ctx, templates
from kodji.apps.web._gating import refuse_if_unpaid
from kodji.config import settings
from kodji.db import connect
from kodji.services import accounts as accounts_svc
from kodji.store import push as push_repo

router = APIRouter()

_MANIFEST = STATIC_DIR / "manifest.webmanifest"
_SW = STATIC_DIR / "sw.js"


@router.get("/manifest.webmanifest")
def manifest() -> Response:
    return Response(
        _MANIFEST.read_bytes(),
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/sw.js")
def service_worker() -> Response:
    body = _SW.read_text(encoding="utf-8").replace("__STATIC_V__", STATIC_VERSION)
    return Response(
        body,
        media_type="application/javascript; charset=utf-8",
        headers={
            # Cloudflare would otherwise cache a `.js` at the edge for
            # hours and phones would keep running the old worker; browsers
            # already re-check a worker at most every 24 h on their own.
            "Cache-Control": "no-cache, max-age=0",
            "Service-Worker-Allowed": "/",
        },
    )


@router.get("/offline", response_class=HTMLResponse)
def offline(request: Request):
    return templates.TemplateResponse(request, "offline.html", base_ctx(request))


# ---------------------------------------------------------------------------
# Subscription API
# ---------------------------------------------------------------------------


@router.get("/api/push/config")
def push_config() -> JSONResponse:
    """What the client needs before it can subscribe. The public key is
    public by definition — it is embedded in every subscription."""
    return JSONResponse(
        {"enabled": settings.has_push, "public_key": settings.vapid_public_key or None}
    )


def _sign_in_required() -> JSONResponse:
    return JSONResponse({"error": "sign_in_required"}, status_code=401)


async def _json_body(request: Request) -> dict | None:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@router.post("/api/push/subscribe")
async def subscribe(request: Request):
    """Store the browser's `PushSubscription.toJSON()` for this user."""
    identity = accounts_svc.identity_for(request)
    if identity is None:
        return _sign_in_required()
    if (refused := refuse_if_unpaid(request, feature="Alerts")) is not None:
        return refused
    if not settings.has_push:
        return JSONResponse({"error": "push_not_configured"}, status_code=503)
    data = await _json_body(request)
    keys = (data or {}).get("keys") or {}
    endpoint = (data or {}).get("endpoint")
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not (isinstance(endpoint, str) and endpoint.startswith("https://")):
        return JSONResponse({"error": "bad_endpoint"}, status_code=400)
    if not (isinstance(p256dh, str) and isinstance(auth, str) and p256dh and auth):
        return JSONResponse({"error": "bad_keys"}, status_code=400)
    if len(endpoint) > 2048:
        return JSONResponse({"error": "bad_endpoint"}, status_code=400)
    with connect(settings.db_path) as conn:
        push_repo.upsert(
            conn,
            user_id=identity.user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=request.headers.get("user-agent", ""),
        )
        devices = push_repo.count_for_user(conn, identity.user_id)
    return JSONResponse({"ok": True, "devices": devices})


@router.delete("/api/push/subscribe")
async def unsubscribe(request: Request):
    """Forget one device. Idempotent: unknown endpoints are a no-op."""
    identity = accounts_svc.identity_for(request)
    if identity is None:
        return _sign_in_required()
    data = await _json_body(request)
    endpoint = (data or {}).get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return JSONResponse({"error": "bad_endpoint"}, status_code=400)
    with connect(settings.db_path) as conn:
        removed = push_repo.delete_endpoint(conn, identity.user_id, endpoint)
        devices = push_repo.count_for_user(conn, identity.user_id)
    return JSONResponse({"ok": True, "removed": removed, "devices": devices})
