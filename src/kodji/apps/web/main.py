"""FastAPI entry point for kodji-terminal.

Wires the page/fragment/api routers, mounts static assets, and starts the
APScheduler alongside the app via FastAPI's lifespan context.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from kodji import __version__
from kodji.apps.web._common import STATIC_DIR
from kodji.apps.web.routes import api, auth, fragments, pages
from kodji.jobs.scheduler import build_scheduler
from kodji.logging import get
from kodji.services.accounts import NotAuthenticated

log = get(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sched = build_scheduler()
    sched.start()
    log.info("scheduler started: %s", [j.id for j in sched.get_jobs()])
    try:
        yield
    finally:
        try:
            sched.shutdown(wait=False)
            log.info("scheduler shut down")
        except Exception as e:  # pragma: no cover - defensive
            log.warning("scheduler shutdown failed: %s", e)


app = FastAPI(title="kodji-terminal", version=__version__, lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(fragments.router)
app.include_router(api.router)


@app.exception_handler(NotAuthenticated)
def _needs_sign_in(request: Request, exc: NotAuthenticated):
    """Turn the service-layer auth error into a trip to /login.

    `services/accounts` raises a plain exception so it stays free of a web
    framework; translating it to a redirect is this layer's job. Only
    reachable once `AUTH_REQUIRED` is on.
    """
    del exc
    # An HTMX fragment must not be answered with a redirect — htmx would
    # swap the login page into whatever div made the request. `HX-Redirect`
    # tells it to navigate the whole browser instead.
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse(url="/login", status_code=303)
