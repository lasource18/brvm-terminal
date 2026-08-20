"""Shared httpx client for all scrapers.

Sets a custom User-Agent (identifies us to source ops per CLAUDE.md
scraper etiquette), a modest timeout, and follow-redirects. Callers open
one client and reuse it for a batch of requests.
"""

from __future__ import annotations

import httpx

from brvm.config import settings


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": settings.http_user_agent,
            "Accept-Language": "fr,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=settings.http_timeout_s,
        follow_redirects=True,
    )
