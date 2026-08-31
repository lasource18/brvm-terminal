"""HTMX fragment endpoints."""

from __future__ import annotations

from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from kodji.apps.web._common import templates
from kodji.clock import is_market_open, utc_iso
from kodji.models import AlertRule
from kodji.services import accounts as accounts_svc
from kodji.services import alerts as alerts_svc
from kodji.services import directory, market, search, watchlist
from kodji.services import news as news_svc


def _wl_ctx(request: Request, slug: str) -> dict:
    return {
        "wl": watchlist.get_with_quotes(accounts_svc.current_account_id(request), slug),
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
        watchlist.create(accounts_svc.current_account_id(request), name.strip())
    except Exception as e:  # sqlite IntegrityError etc.
        raise HTTPException(status_code=400, detail=str(e)) from e
    return templates.TemplateResponse(
        request,
        "_frag/watchlists_index.html",
        {"watchlists": watchlist.list_all(accounts_svc.current_account_id(request))},
    )


@router.get("/watchlists/{slug}", response_class=HTMLResponse)
def watchlist_body_frag(request: Request, slug: str):
    try:
        return templates.TemplateResponse(request, "_frag/watchlist.html", _wl_ctx(request, slug))
    except watchlist.WatchlistNotFound as e:
        raise HTTPException(status_code=404, detail=f"unknown watchlist: {slug}") from e


@router.post("/watchlists/{slug}/items", response_class=HTMLResponse)
def add_watchlist_item(request: Request, slug: str, ticker: str = Form(...)):
    try:
        watchlist.add_item(accounts_svc.current_account_id(request), slug, ticker)
    except watchlist.WatchlistNotFound as e:
        raise HTTPException(status_code=404, detail=f"unknown watchlist: {slug}") from e
    except watchlist.TickerUnknown as e:
        raise HTTPException(
            status_code=400, detail=f"unknown ticker: {ticker.upper()}"
        ) from e
    return templates.TemplateResponse(request, "_frag/watchlist.html", _wl_ctx(request, slug))


@router.delete("/watchlists/{slug}/items/{ticker}", response_class=HTMLResponse)
def remove_watchlist_item(request: Request, slug: str, ticker: str):
    try:
        watchlist.remove_item(accounts_svc.current_account_id(request), slug, ticker)
    except watchlist.WatchlistNotFound as e:
        raise HTTPException(status_code=404, detail=f"unknown watchlist: {slug}") from e
    return templates.TemplateResponse(request, "_frag/watchlist.html", _wl_ctx(request, slug))


@router.get("/search", response_class=HTMLResponse)
def search_frag(request: Request, q: str = ""):
    hits = search.search(q, limit=8)
    return templates.TemplateResponse(
        request, "_frag/search_results.html", {"hits": hits}
    )


def _news_canonical_url(
    *,
    ticker: str | None,
    category: str | None,
    date_from: str | None,
    date_to: str | None,
    min_relevance: int | None,
) -> str:
    """Canonical `/news?...` URL for a given filter set.

    Feeds the `HX-Push-Url` response header on the news fragment so a
    filtered view is shareable — pasting the URL into a fresh tab
    lands on the same filtered `/news` page, not the fragment endpoint.
    Empty filters are dropped so `/news` (bare) stays the canonical URL
    for an unfiltered view.
    """
    params: list[tuple[str, str]] = []
    if ticker:
        params.append(("ticker", ticker))
    if category:
        params.append(("category", category))
    if date_from:
        params.append(("date_from", date_from))
    if date_to:
        params.append(("date_to", date_to))
    if min_relevance is not None:
        params.append(("min_relevance", str(min_relevance)))
    if not params:
        return "/news"
    return f"/news?{urlencode(params)}"


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
    response = templates.TemplateResponse(
        request,
        "_frag/news_feed.html",
        {"feed": feed, "feed_url": "/_frag/news"},
    )
    # `HX-Push-Url` overrides HTMX's default push (which would be the
    # fragment URL) with a canonical `/news?...` — a URL a user can
    # share or bookmark. Scoped to the standalone `/news` page via the
    # `HX-Current-URL` header; per-ticker News tabs (`/s/{ticker}/news`)
    # keep their ticker-scoped URL because a push would erase it.
    hx_current = request.headers.get("hx-current-url", "")
    current_path = urlparse(hx_current).path if hx_current else ""
    if current_path == "/news":
        response.headers["HX-Push-Url"] = _news_canonical_url(
            ticker=ticker or None,
            category=category or None,
            date_from=date_from or None,
            date_to=date_to or None,
            min_relevance=min_relevance,
        )
    return response


def _rules_ctx(request: Request) -> dict:
    return {"rules": alerts_svc.list_rules(accounts_svc.current_account_id(request))}


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
    # F-21: parse threshold_pct inside a try — the previous `float(...)`
    # ran ahead of the outer try block, so `abc` returned a 500 instead
    # of a 400. Also gate on threshold > 0: a 0 threshold matches every
    # move and turns any price_move rule into a fire-on-everything.
    thr: float | None = None
    if kind == "price_move" and threshold_pct:
        try:
            thr = float(threshold_pct)
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"threshold_pct must be numeric, got {threshold_pct!r}",
            ) from e
        if thr <= 0:
            raise HTTPException(
                status_code=400,
                detail="threshold_pct must be > 0 (0 fires on every move)",
            )
    if kind == "price_move" and thr is None:
        raise HTTPException(status_code=400, detail="price_move needs threshold_pct")
    docs = (doc_types or "").strip() or None if kind == "new_filing" else None
    rel: int | None = None
    if kind == "news" and min_relevance:
        try:
            rel = int(min_relevance)
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"min_relevance must be integer, got {min_relevance!r}",
            ) from e
    try:
        alerts_svc.create_rule(
            accounts_svc.current_account_id(request),
            AlertRule(
                kind=kind, ticker=t, label=lbl,
                threshold_pct=thr, doc_types=docs, min_relevance=rel,
            )
        )
    except Exception as e:  # sqlite IntegrityError (FK on ticker) etc.
        raise HTTPException(status_code=400, detail=str(e)) from e
    return templates.TemplateResponse(request, "_frag/alerts_rules.html", _rules_ctx(request))


@router.post("/alerts/rules/{rule_id}/toggle", response_class=HTMLResponse)
def toggle_alert_rule(request: Request, rule_id: int):
    from kodji.config import settings as _s
    from kodji.db import connect
    from kodji.store import alerts as _alerts_repo

    account_id = accounts_svc.current_account_id(request)
    with connect(_s.db_path) as conn:
        # Scoped read: a rule_id from another account's URL must 404 here,
        # not toggle someone else's alert.
        rule = _alerts_repo.get_rule(conn, account_id, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=f"unknown rule: {rule_id}")
        _alerts_repo.set_enabled(conn, account_id, rule_id, not rule.enabled)
    return templates.TemplateResponse(request, "_frag/alerts_rules.html", _rules_ctx(request))


@router.delete("/alerts/rules/{rule_id}", response_class=HTMLResponse)
def delete_alert_rule(request: Request, rule_id: int):
    n = alerts_svc.delete_rule(accounts_svc.current_account_id(request), rule_id)
    if n == 0:
        raise HTTPException(status_code=404, detail=f"unknown rule: {rule_id}")
    return templates.TemplateResponse(request, "_frag/alerts_rules.html", _rules_ctx(request))


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
