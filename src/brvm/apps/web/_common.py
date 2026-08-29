"""Shared helpers for the web layer."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from jinja2.runtime import Context

from brvm import __version__
from brvm.clock import is_market_open, now_abidjan
from brvm.i18n import DEFAULT_LOCALE, Locale, normalize, translate

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
# Cookie name used to persist the user's locale choice across sessions.
# Single-user app — no CSRF concern with a preference cookie.
LANG_COOKIE = "brvm_lang"
# Twelve months. The setting is a pure UI preference; expiring it aggressively
# would just make the toggle feel forgetful.
LANG_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@pass_context
def _t(ctx: Context, source: str) -> str:
    """Jinja `t` filter — translates `source` using the per-render `locale`
    from the template context (populated by `base_ctx`).

    `pass_context` is Jinja's way of giving a filter access to the render
    context, which keeps the locale request-scoped instead of a module
    global (fragment renders that don't go through `base_ctx` fall back
    to the default locale, and concurrent requests can't clobber each
    other's language)."""
    locale = ctx.get("locale", DEFAULT_LOCALE)
    return translate(source, locale)


templates.env.filters["t"] = _t


def resolve_locale(request: Request) -> Locale:
    """Resolve the effective locale for this request.

    Priority: `?lang=` query (explicit user action) → cookie (persisted
    preference) → default. Query wins so a shareable URL can force a
    locale without clobbering the cookie."""
    qs = request.query_params.get("lang")
    if qs:
        return normalize(qs)
    return normalize(request.cookies.get(LANG_COOKIE))


def base_ctx(request: Request) -> dict:
    """Common template context injected into every full-page render."""
    locale = resolve_locale(request)
    return {
        "version": __version__,
        "abidjan_now": now_abidjan().strftime("%Y-%m-%d %H:%M %Z"),
        "market_open": is_market_open(),
        "locale": locale,
        # Powers the FR|EN toggle links so clicking a language returns
        # the reader to the same page they're on.
        "current_path": request.url.path,
    }
