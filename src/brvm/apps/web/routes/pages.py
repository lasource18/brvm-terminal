"""Full-page HTML routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from brvm import __version__
from brvm.apps.web import tabs
from brvm.apps.web._common import base_ctx, templates
from brvm.clock import is_market_open, utc_iso
from brvm.services import company, directory, market, watchlist

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "overview.html",
        {**base_ctx(), "overview": market.overview(limit=10)},
    )


@router.get("/s/{ticker}", response_class=HTMLResponse)
def security_root(ticker: str):
    # Deep-link stability: redirect the bare URL to the Overview tab.
    return RedirectResponse(url=f"/s/{ticker}/overview", status_code=307)


@router.get("/s/{ticker}/{tab}", response_class=HTMLResponse)
def security_tab(request: Request, ticker: str, tab: str):
    spec = tabs.get(tab)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown tab: {tab}")
    sec = market.get_security(ticker)
    if sec is None:
        raise HTTPException(status_code=404, detail=f"unknown ticker: {ticker}")
    ctx = {
        **base_ctx(),
        "sec": sec,
        "tabs": tabs.TABS,
        "active_tab": spec.key,
        "tab": spec,
        "tab_template": spec.template,
    }
    if spec.key == "description":
        ctx["profile"] = company.get_description(sec.ticker)
    elif spec.key == "peers":
        ctx["peers"] = company.get_peers(sec.ticker)
    return templates.TemplateResponse(request, "security.html", ctx)


@router.get("/directory", response_class=HTMLResponse)
def directory_page(
    request: Request,
    country: str | None = Query(default=None),
    sector: str | None = Query(default=None),
    q: str | None = Query(default=None),
    kind: str | None = Query(default=None),
):
    return templates.TemplateResponse(
        request,
        "directory.html",
        {
            **base_ctx(),
            "rows": directory.list_directory(country=country, sector=sector, q=q, kind=kind),
            "countries": directory.distinct_countries(),
            "sectors": directory.distinct_sectors(),
            "current": {"country": country or "", "sector": sector or "", "q": q or "", "kind": kind or ""},
        },
    )


@router.get("/watchlists", response_class=HTMLResponse)
def watchlists_index(request: Request):
    return templates.TemplateResponse(
        request,
        "watchlists.html",
        {**base_ctx(), "watchlists": watchlist.list_all()},
    )


@router.get("/watchlists/{slug}", response_class=HTMLResponse)
def watchlist_page(request: Request, slug: str):
    try:
        wl = watchlist.get_with_quotes(slug)
    except watchlist.WatchlistNotFound as e:
        raise HTTPException(status_code=404, detail=f"unknown watchlist: {slug}") from e
    return templates.TemplateResponse(
        request,
        "watchlist.html",
        {
            **base_ctx(),
            "wl": wl,
            "market_open": is_market_open(),
            "generated_utc": utc_iso(),
        },
    )


@router.get("/health")
def health():
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "utc": utc_iso(),
            "market_open": is_market_open(),
        }
    )
