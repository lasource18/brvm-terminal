"""Full-page HTML routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from kodji import __version__
from kodji.apps.web import tabs
from kodji.apps.web._common import (
    LANG_COOKIE,
    LANG_COOKIE_MAX_AGE,
    base_ctx,
    templates,
)
from kodji.clock import is_market_open, utc_iso
from kodji.config import settings
from kodji.i18n import normalize
from kodji.services import (
    alerts as alerts_svc,
)
from kodji.services import analyst_notes as notes_svc
from kodji.services import bonds as bonds_svc
from kodji.services import brief as brief_svc
from kodji.services import company, directory, fundamentals, market, ratios, watchlist
from kodji.services import news as news_svc

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "overview.html",
        {**base_ctx(request), "overview": market.overview(limit=10)},
    )


@router.get("/lang/{code}")
def set_locale(code: str, next: str = "/"):
    """Persist the user's locale choice in a cookie, then redirect back.

    Called from the FR|EN toggle in `base.html`. `next` is the URL to
    return to; we constrain it to a same-origin path so the toggle can't
    be turned into an open-redirect vector (the topbar always passes the
    current path, so this only rejects hand-crafted attacks). Unknown
    codes fall back to the default locale rather than 4xx-ing — the UI
    should never present a language button it can't accept."""
    resolved = normalize(code)
    # Only allow same-origin, path-only redirects. Absolute URLs, "//host"
    # schemes, or anything starting with a scheme are dropped to root.
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(url=safe_next, status_code=303)
    resp.set_cookie(
        LANG_COOKIE, resolved,
        max_age=LANG_COOKIE_MAX_AGE,
        httponly=False,   # a preference — not a session token; keep it visible to JS
        samesite="lax",
    )
    return resp


@router.get("/s/{ticker}", response_class=HTMLResponse)
def security_root(ticker: str):
    # Kind-aware redirect: bonds land on Overview (their DES-equivalent),
    # equities + indices land on Chart. Unknown tickers still 307 to
    # /chart which surfaces a clean 404 from the tab route.
    sec = market.get_security(ticker)
    kind = sec.kind if sec else None
    return RedirectResponse(url=f"/s/{ticker}/{tabs.default_tab_for(kind)}", status_code=307)


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
        **base_ctx(request),
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
        # Per-ticker feed: matches ticker_hint OR the LLM-tagged CSV for
        # equities/indices. Bonds have no equity ticker in the tagger's
        # vocabulary, so `services.bonds.list_issuer_news` bridges via
        # issuer_name substring — imperfect but non-zero coverage.
        if sec.kind == "bond":
            ctx["feed"] = news_svc.list_feed_from_rows(
                bonds_svc.list_issuer_news(sec.ticker, limit=25)
            )
        else:
            ctx["feed"] = news_svc.list_feed(ticker=sec.ticker, limit=25)
    elif spec.key in {"overview", "cashflow", "yield", "related"}:
        # Phase 8c: bond tabs share a single view-model that composes
        # reference data + latest snapshot + derived schedule + yield
        # summary + related list in one query burst.
        ctx["bond"] = bonds_svc.get_bond_view(sec.ticker)
    elif spec.key == "corporate-actions":
        ctx["actions"] = news_svc.list_upcoming_actions(ticker=sec.ticker, days=90)
    elif spec.key == "financials":
        ctx["financials"] = fundamentals.get_financials_series(sec.ticker)
        ctx["interim"] = fundamentals.get_latest_interim(sec.ticker)
        ctx["ratios_series"] = ratios.get_ratios_series(sec.ticker)
        ctx["interim_ratios"] = ratios.get_ratios_for_interim(sec.ticker)
        ctx["filing_refs"] = fundamentals.get_financials_source_filings(sec.ticker)
    elif spec.key == "ownership":
        ctx["ownership"] = fundamentals.get_ownership(sec.ticker)
    elif spec.key == "segments":
        ctx["segments"] = fundamentals.get_segments(sec.ticker)
    elif spec.key == "analyst":
        # Phase 6c: latest weekly note. Archive sidebar lists prior
        # weeks so a reader can walk backwards without leaving the tab.
        note = notes_svc.latest_note(sec.ticker)
        ctx["note"] = note
        if note:
            md, pending = _pick_localized_markdown(
                note.markdown, note.markdown_fr, ctx["locale"],
            )
            ctx["note_html"] = _render_markdown(md)
            ctx["translation_pending"] = pending
        else:
            ctx["note_html"] = ""
            ctx["translation_pending"] = False
        ctx["archive"] = notes_svc.list_notes(sec.ticker, limit=12)
    return templates.TemplateResponse(request, "security.html", ctx)


@router.get("/s/{ticker}/analyst/{week_start}", response_class=HTMLResponse)
def analyst_note_by_week(request: Request, ticker: str, week_start: str):
    """Archive route — a specific historical note for a ticker."""
    from datetime import date as _date
    try:
        _date.fromisoformat(week_start)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=f"bad week: {week_start}") from e
    sec = market.get_security(ticker)
    if sec is None:
        raise HTTPException(status_code=404, detail=f"unknown ticker: {ticker}")
    # F-33: the tab-route hides analyst for both indices AND bonds
    # (the tab spec is equity-only), but this archive route only
    # blocked indices. A hand-crafted `/s/EOM.O10/analyst/2026-08-24`
    # would 404 on "no note" today but would render happily if a bond
    # note ever landed in the DB — inconsistent gating between the
    # two entry points.
    if sec.kind in {"index", "bond"}:
        raise HTTPException(
            status_code=404,
            detail=f"analyst view is not available for {sec.kind}s",
        )
    note = notes_svc.get_note(sec.ticker, week_start)
    if note is None:
        raise HTTPException(
            status_code=404, detail=f"no analyst note for {ticker} week {week_start}"
        )
    spec = tabs.get("analyst")
    base = base_ctx(request)
    md, pending = _pick_localized_markdown(
        note.markdown, note.markdown_fr, base["locale"],
    )
    ctx = {
        **base,
        "sec": sec,
        "tabs": tabs.visible_for(sec.kind),
        "active_tab": "analyst",
        "tab": spec,
        "tab_template": spec.template,
        "note": note,
        "note_html": _render_markdown(md),
        "translation_pending": pending,
        "archive": notes_svc.list_notes(sec.ticker, limit=12),
    }
    return templates.TemplateResponse(request, "security.html", ctx)


def _render_markdown(md: str) -> str:
    """Server-side markdown → HTML with `html=False` so any raw HTML in
    the source is escaped. Same rendering config as the /brief route.

    F-39: `linkify: True` in the options dict alone is inert —
    markdown-it-py requires an explicit `.enable("linkify")` before the
    renderer treats bare URLs as anchors. Without both, URLs in briefs
    and analyst notes rendered as plain text and readers had to copy-
    paste. Verified empirically before/after: `.enable("linkify")` is
    what makes `<a href="...">` appear.
    """
    from markdown_it import MarkdownIt
    return (
        MarkdownIt("commonmark", {"html": False, "linkify": True})
        .enable("linkify")
        .render(md)
    )


def _pick_localized_markdown(
    source: str, translation: str | None, locale: str
) -> tuple[str, bool]:
    """Pick brief/note markdown for `locale` and return (markdown, pending).

    PR-I: EN is the source language (brief + note prompts both pin
    English output). FR is a cached translation on `markdown_fr`. When
    the reader asks for FR but the translation hasn't landed yet we
    fall back to the source with `pending=True` so the template can
    render a "translation pending" badge.
    """
    if locale == "fr" and translation:
        return translation, False
    if locale == "fr":
        return source, True
    return source, False


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
            **base_ctx(request),
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
            **base_ctx(request),
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
        {**base_ctx(request), "watchlists": watchlist.list_all()},
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
            **base_ctx(request),
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
            **base_ctx(request),
            "rules": alerts_svc.list_rules(),
            "events": alerts_svc.list_recent_events(limit=25),
            "has_discord": settings.has_discord,
        },
    )


def _render_brief_page(request: Request, brief) -> HTMLResponse:
    base = base_ctx(request)
    body_html = ""
    translation_pending = False
    if brief:
        md, translation_pending = _pick_localized_markdown(
            brief.markdown, brief.markdown_fr, base["locale"],
        )
        body_html = _render_markdown(md)
    return templates.TemplateResponse(
        request,
        "brief.html",
        {
            **base,
            "brief": brief,
            "body_html": body_html,
            "translation_pending": translation_pending,
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
