"""HTMX fragment endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from brvm.apps.web._common import templates
from brvm.clock import is_market_open, utc_iso
from brvm.services import directory, market, search, watchlist
from brvm.services import news as news_svc


def _wl_ctx(slug: str) -> dict:
    return {
        "wl": watchlist.get_with_quotes(slug),
        "market_open": is_market_open(),
        "generated_utc": utc_iso(),
    }

router = APIRouter(prefix="/_frag")


@router.get("/overview", response_class=HTMLResponse)
def overview_frag(request: Request):
    return templates.TemplateResponse(
        request,
        "_frag/overview.html",
        {"overview": market.overview(limit=10)},
    )


@router.post("/watchlists", response_class=HTMLResponse)
def create_watchlist(request: Request, name: str = Form(...)):
    try:
        watchlist.create(name.strip())
    except Exception as e:  # sqlite IntegrityError etc.
        raise HTTPException(status_code=400, detail=str(e)) from e
    return templates.TemplateResponse(
        request,
        "_frag/watchlists_index.html",
        {"watchlists": watchlist.list_all()},
    )


@router.get("/watchlists/{slug}", response_class=HTMLResponse)
def watchlist_body_frag(request: Request, slug: str):
    try:
        return templates.TemplateResponse(request, "_frag/watchlist.html", _wl_ctx(slug))
    except watchlist.WatchlistNotFound as e:
        raise HTTPException(status_code=404, detail=f"unknown watchlist: {slug}") from e


@router.post("/watchlists/{slug}/items", response_class=HTMLResponse)
def add_watchlist_item(request: Request, slug: str, ticker: str = Form(...)):
    try:
        watchlist.add_item(slug, ticker)
    except watchlist.WatchlistNotFound as e:
        raise HTTPException(status_code=404, detail=f"unknown watchlist: {slug}") from e
    except watchlist.TickerUnknown as e:
        raise HTTPException(
            status_code=400, detail=f"unknown ticker: {ticker.upper()}"
        ) from e
    return templates.TemplateResponse(request, "_frag/watchlist.html", _wl_ctx(slug))


@router.delete("/watchlists/{slug}/items/{ticker}", response_class=HTMLResponse)
def remove_watchlist_item(request: Request, slug: str, ticker: str):
    try:
        watchlist.remove_item(slug, ticker)
    except watchlist.WatchlistNotFound as e:
        raise HTTPException(status_code=404, detail=f"unknown watchlist: {slug}") from e
    return templates.TemplateResponse(request, "_frag/watchlist.html", _wl_ctx(slug))


@router.get("/search", response_class=HTMLResponse)
def search_frag(request: Request, q: str = ""):
    hits = search.search(q, limit=8)
    return templates.TemplateResponse(
        request, "_frag/search_results.html", {"hits": hits}
    )


@router.get("/news", response_class=HTMLResponse)
def news_frag(
    request: Request,
    ticker: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_relevance: int | None = None,
    limit: int = 25,
    offset: int = 0,
):
    feed = news_svc.list_feed(
        ticker=ticker or None,
        category=category or None,
        date_from=date_from or None,
        date_to=date_to or None,
        min_relevance=min_relevance,
        limit=max(1, min(100, limit)),
        offset=max(0, offset),
    )
    return templates.TemplateResponse(
        request,
        "_frag/news_feed.html",
        {"feed": feed, "feed_url": "/_frag/news"},
    )


@router.get("/directory", response_class=HTMLResponse)
def directory_frag(
    request: Request,
    country: str | None = None,
    sector: str | None = None,
    q: str | None = None,
    kind: str | None = None,
):
    return templates.TemplateResponse(
        request,
        "_frag/directory_body.html",
        {"rows": directory.list_directory(country=country, sector=sector, q=q, kind=kind)},
    )
