"""HTMX fragment endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from brvm.apps.web._common import templates
from brvm.clock import is_market_open, utc_iso
from brvm.models import AlertRule
from brvm.services import alerts as alerts_svc
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


def _rules_ctx() -> dict:
    return {"rules": alerts_svc.list_rules()}


def _events_ctx() -> dict:
    return {"events": alerts_svc.list_recent_events(limit=25)}


@router.post("/alerts/rules", response_class=HTMLResponse)
def create_alert_rule(
    request: Request,
    kind: str = Form(...),
    ticker: str = Form(default=""),
    label: str = Form(default=""),
    threshold_pct: str = Form(default=""),
    doc_types: str = Form(default=""),
    min_relevance: str = Form(default=""),
):
    if kind not in {"price_move", "new_filing", "news"}:
        raise HTTPException(status_code=400, detail=f"unknown kind: {kind}")
    t = (ticker or "").strip().upper() or None
    lbl = (label or "").strip() or None
    thr = float(threshold_pct) if kind == "price_move" and threshold_pct else None
    if kind == "price_move" and thr is None:
        raise HTTPException(status_code=400, detail="price_move needs threshold_pct")
    docs = (doc_types or "").strip() or None if kind == "new_filing" else None
    rel = int(min_relevance) if kind == "news" and min_relevance else None
    try:
        alerts_svc.create_rule(
            AlertRule(
                kind=kind, ticker=t, label=lbl,
                threshold_pct=thr, doc_types=docs, min_relevance=rel,
            )
        )
    except Exception as e:  # sqlite IntegrityError (FK on ticker) etc.
        raise HTTPException(status_code=400, detail=str(e)) from e
    return templates.TemplateResponse(request, "_frag/alerts_rules.html", _rules_ctx())


@router.post("/alerts/rules/{rule_id}/toggle", response_class=HTMLResponse)
def toggle_alert_rule(request: Request, rule_id: int):
    from brvm.config import settings as _s
    from brvm.db import connect
    from brvm.store import alerts as _alerts_repo

    with connect(_s.db_path) as conn:
        rule = _alerts_repo.get_rule(conn, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=f"unknown rule: {rule_id}")
        _alerts_repo.set_enabled(conn, rule_id, not rule.enabled)
    return templates.TemplateResponse(request, "_frag/alerts_rules.html", _rules_ctx())


@router.delete("/alerts/rules/{rule_id}", response_class=HTMLResponse)
def delete_alert_rule(request: Request, rule_id: int):
    n = alerts_svc.delete_rule(rule_id)
    if n == 0:
        raise HTTPException(status_code=404, detail=f"unknown rule: {rule_id}")
    return templates.TemplateResponse(request, "_frag/alerts_rules.html", _rules_ctx())


@router.get("/directory", response_class=HTMLResponse)
def directory_frag(
    request: Request,
    country: str | None = None,
    sector: str | None = None,
    q: str | None = None,
    kind: str | None = None,
    sort: str | None = None,
    direction: str | None = None,
):
    return templates.TemplateResponse(
        request,
        "_frag/directory_body.html",
        {
            "rows": directory.list_directory(
                country=country, sector=sector, q=q, kind=kind,
                sort=sort, direction=direction,
            ),
            # `current` is needed so the header links in the fragment
            # preserve the active filters + toggle the sort direction.
            "current": {
                "country": country or "", "sector": sector or "",
                "q": q or "", "kind": kind or "",
                "sort": sort or "", "direction": (direction or "").lower(),
            },
        },
    )
