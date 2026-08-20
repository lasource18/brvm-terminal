"""Shared helper for computing a stable dedupe hash for news / communiqués.

Every source module computes the same hash so cross-source duplicates
(same URL surfaced by multiple aggregators) collapse to one row.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

_WS_RE = re.compile(r"\s+")


def _normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    # Drop query + fragment; lowercase everything. Sikafinance URLs are
    # already lower-case, so treating paths case-insensitively catches
    # cosmetic variance without creating false collisions.
    path = re.sub(r"/+", "/", parts.path.lower()).rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def _normalize_title(title: str) -> str:
    return _WS_RE.sub(" ", title.strip()).lower()


def news_hash(url: str, title: str) -> str:
    """sha256(normalized_url + '|' + normalized_title) as a hex string."""
    payload = f"{_normalize_url(url)}|{_normalize_title(title)}".encode()
    return hashlib.sha256(payload).hexdigest()
