"""FastAPI entry point for kodji-terminal.

Wires the page/fragment/api routers, mounts static assets, and starts the
APScheduler alongside the app via FastAPI's lifespan context.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from kodji import __version__
from kodji.apps.web._common import STATIC_DIR
from kodji.apps.web.routes import api, fragments, pages
from kodji.jobs.scheduler import build_scheduler
from kodji.logging import get

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
app.include_router(pages.router)
app.include_router(fragments.router)
app.include_router(api.router)
