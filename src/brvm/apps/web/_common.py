"""Shared helpers for the web layer."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from brvm import __version__
from brvm.clock import is_market_open, now_abidjan

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def base_ctx() -> dict:
    """Common template context injected into every full-page render."""
    return {
        "version": __version__,
        "abidjan_now": now_abidjan().strftime("%Y-%m-%d %H:%M %Z"),
        "market_open": is_market_open(),
    }
