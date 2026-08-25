"""Full-page HTML routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from brvm import __version__
from brvm.apps.web import tabs
from brvm.apps.web._common import base_ctx, templates
from brvm.clock import is_market_open, utc_iso
from brvm.config import settings
from brvm.services import (
    alerts as alerts_svc,
)
from brvm.services import brief as brief_svc
from brvm.services import company, directory, fundamentals, market, ratios, watchlist
from brvm.services import news as news_svc

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
    # Deep-link stability: redirect the bare URL to the Chart tab.
    return RedirectResponse(url=f"/s/{ticker}/chart", status_code=307)


@router.get("/s/{ticker}/{tab}", response_class=HTMLResponse)
def security_tab(request: Request, ticker: str, tab: str):
    spec = tabs.get(tab)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown tab: {tab}")
    sec = market.get_security(ticker)
    if sec is None:
        raise HTTPException(status_code=404, detail=f"unknown ticker: {ticker}")
    if sec.kind in spec.hidden_for_kinds:
        raise HTTPException(
            status_code=404, detail=f"tab {tab!r} not available for {sec.kind}"
        )
    ctx = {
        **base_ctx(),
        "sec": sec,
        "tabs": tabs.visible_for(sec.kind),
        "active_tab": spec.key,
        "tab": spec,
        "tab_template": spec.template,
    }
    if spec.key == "description":
        ctx["profile"] = company.get_description(sec.ticker)
    elif spec.key == "peers":
        # Phase 4d: peers now come annotated with P/E, ROE, net margin
        # sourced from `services/ratios` so the tab supports cross-ticker
        # comparison in-place.
        ctx["peers"] = company.get_peers_with_ratios(sec.ticker)
    elif spec.key == "news":
        # Per-ticker feed: matches ticker_hint OR the LLM-tagged CSV.
        ctx["feed"] = news_svc.list_feed(ticker=sec.ticker, limit=25)
    elif spec.key == "corporate-actions":
        ctx["actions"] = news_svc.list_upcoming_actions(ticker=sec.ticker, days=90)
    elif spec.key == "financials":
        ctx["financials"] = fundamentals.get_financials_series(sec.ticker)
        ctx["interim"] = fundamentals.get_latest_interim(sec.ticker)
        ctx["ratios_series"] = ratios.get_ratios_series(sec.ticker)
        ctx["interim_ratios"] = ratios.get_ratios_for_interim(sec.ticker)
    elif spec.key == "ownership":
        ctx["ownership"] = fundamentals.get_ownership(sec.ticker)
    elif spec.key == "segments":
        ctx["segments"] = fundamentals.get_segments(sec.ticker)
    return templates.TemplateResponse(request, "security.html", ctx)


@router.get("/news", response_class=HTMLResponse)
def news_page(
    request: Request,
    ticker: str | None = Query(default=None),
    category: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    min_relevance: int | None = Query(default=None, ge=0, le=10),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    feed = news_svc.list_feed(
        ticker=ticker or None,
        category=category or None,
        date_from=date_from or None,
        date_to=date_to or None,
        min_relevance=min_relevance,
        limit=limit,
        offset=offset,
    )
    return templates.TemplateResponse(
        request,
        "news.html",
        {
            **base_ctx(),
            "feed": feed,
            "categories": news_svc.CATEGORIES,
        },
    )


@router.get("/directory", response_class=HTMLResponse)
def directory_page(
    request: Request,
    country: str | None = Query(default=None),
    sector: str | None = Query(default=None),
    q: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    direction: str | None = Query(default=None),
):
    return templates.TemplateResponse(
        request,
        "directory.html",
        {
            **base_ctx(),
            "rows": directory.list_directory(
                country=country, sector=sector, q=q, kind=kind,
                sort=sort, direction=direction,
            ),
            "countries": directory.distinct_countries(),
            "sectors": directory.distinct_sectors(),
            "current": {
                "country": country or "", "sector": sector or "",
                "q": q or "", "kind": kind or "",
                "sort": sort or "", "direction": (direction or "").lower(),
            },
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


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request):
    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            **base_ctx(),
            "rules": alerts_svc.list_rules(),
            "events": alerts_svc.list_recent_events(limit=25),
            "has_discord": settings.has_discord,
        },
    )


def _render_brief_page(request: Request, brief) -> HTMLResponse:
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "linkify": True})
    body_html = md.render(brief.markdown) if brief else ""
    return templates.TemplateResponse(
        request,
        "brief.html",
        {
            **base_ctx(),
            "brief": brief,
            "body_html": body_html,
            "archive": brief_svc.list_recent_briefs(limit=30),
        },
    )


@router.get("/brief", response_class=HTMLResponse)
def brief_latest(request: Request):
    latest = brief_svc.latest_brief()
    return _render_brief_page(request, latest)


@router.get("/brief/{day}", response_class=HTMLResponse)
def brief_by_day(request: Request, day: str):
    # Cheap guard so a "/brief/foo" doesn't reach the SQL layer.
    from datetime import date as _date
    try:
        _date.fromisoformat(day)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=f"unknown brief: {day}") from e
    b = brief_svc.get_brief(day)
    if b is None:
        raise HTTPException(status_code=404, detail=f"no brief for {day}")
    return _render_brief_page(request, b)


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
