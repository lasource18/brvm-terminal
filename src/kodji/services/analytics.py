"""First-party pageview counting. No third-party script, no cookie, no
IP address or user agent kept.

**Why this and not Plausible or Umami.** Hosted Plausible sends visitor
data to someone else and costs money; self-hosting it wants ClickHouse,
and Umami wants Node plus Postgres. This box is a 4 GB VPS with a
< 500 MB RSS budget and the project's whole premise is low complexity.
Counting server-side into the SQLite file that is already there costs one
INSERT per page render and adds no process, no dependency and no script
tag.

**Why server-side rather than a JS beacon.** Nothing to block, nothing to
consent to, and it still counts a reader whose browser refuses scripts.
The cost is that crawlers must be recognised from the request alone, and
that nothing client-side — scroll depth, time on page — can ever be
measured. For "did anyone visit, where did they come from, did they reach
the pricing page" that trade is worth it.

**Crawlers are judged twice.** A name match against `_BOTS` is a
*definite* bot and is never recorded; that list only ever catches
crawlers already known, so it cannot be the whole answer. The second
test is behavioural and needs no name: a browser performing a top-level
navigation always sends `Accept-Language` and an `Accept` that asks for
HTML, and almost nothing automated does both. Requests failing that are
recorded but flagged `is_suspected_bot`, and excluded from headline
figures which report how many they left out.

Flagging rather than dropping is the point. The user agent is not stored,
so a dropped request cannot be reviewed afterwards and a wrong rule would
be undetectable. A flagged one is still on disk, still countable once the
rule is corrected, and visible as a number that does not match the
headline.

**How a visitor is counted without identifying them.** `visitor_hash` is
`sha256(salt + ip + user_agent + day)`, where the salt is random and
belongs to one UTC day. When the day rolls the salt is overwritten in
place and the old one is gone, so that day's hashes can no longer be
re-derived from an IP address nor matched against any other day. They
become opaque counters. Neither the IP nor the user agent is stored at
any point; they exist only inside this function call.

The consequence to remember when reading the numbers: a visitor is
counted once per day, not once ever. Week-over-week "unique visitors"
cannot be summed, and that is deliberate.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from kodji.clock import utc_iso
from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.store import analytics as repo

log = get(__name__)

# Paths that are not pages. `/health` is hit every 5 minutes by the uptime
# monitor and would swamp everything else.
_SKIP_PREFIXES = ("/static/", "/api/", "/_frag/", "/lang/", "/billing/webhook")
# `/offline` is HTML, so unlike the others it is not excluded by content
# type. It is the service worker's fallback page, reachable only when the
# network is gone or by something that parsed `sw.js` and followed the
# URL — which is how it turned up in production traffic. Never a reader
# choosing to read it, so never a pageview.
_SKIP_EXACT = {"/health", "/favicon.ico", "/sw.js", "/manifest.webmanifest", "/offline", "/robots.txt"}

# Substring match on a lowercased user agent. Not exhaustive and cannot
# be — `looks_automated` is what catches the ones nobody has named yet.
_BOTS = (
    "bot", "crawl", "spider", "slurp", "curl", "wget", "python-requests",
    "httpx", "headlesschrome", "phantomjs", "monitoring", "uptime",
    "facebookexternalhit", "whatsapp", "telegram", "preview", "scanner",
    "lighthouse", "pagespeed", "gptbot", "ccbot", "ahrefs", "semrush",
)


def _db_path() -> Path:
    return Path(settings.db_path)


def _today() -> str:
    # Africa/Abidjan is UTC+0, so this is also the local trading day.
    return datetime.now(UTC).strftime("%Y-%m-%d")


# Cached so the salt is read once per process per day rather than on every
# request. The DB remains the source of truth, which is what keeps a
# restart from silently minting a second salt for the same day and
# double-counting every visitor already seen.
_salt_cache: tuple[str, str] | None = None


def _salt_for(conn: sqlite3.Connection, day: str) -> str:
    global _salt_cache
    if _salt_cache is not None and _salt_cache[0] == day:
        return _salt_cache[1]
    current = repo.get_salt(conn)
    if current is None or current[0] != day:
        salt = secrets.token_hex(32)
        repo.put_salt(conn, day, salt)
    else:
        salt = current[1]
    _salt_cache = (day, salt)
    return salt


def reset_salt_cache() -> None:
    """Drop the in-process salt cache — for tests, which swap DB paths."""
    global _salt_cache
    _salt_cache = None


def _visitor_hash(salt: str, ip: str, user_agent: str, day: str) -> str:
    raw = f"{salt}|{ip}|{user_agent}|{day}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def client_ip(headers: dict[str, str], fallback: str | None) -> str:
    """The visitor's address, for hashing only — never stored.

    Cloudflare terminates TLS, so `request.client.host` is a Cloudflare
    edge address and would collapse whole regions onto one hash.
    `CF-Connecting-IP` is the real one; `X-Forwarded-For` is the fallback
    for a direct origin hit, taking the first entry (the client; the rest
    are proxies).
    """
    cf = headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return fallback or "-"


def is_bot(user_agent: str) -> bool:
    """A crawler we can name. Never recorded at all."""
    ua = user_agent.lower()
    return not ua or any(token in ua for token in _BOTS)


def looks_automated(accept: str, accept_language: str) -> bool:
    """A crawler we cannot name, judged by how it asks rather than who it
    says it is. Recorded, but flagged.

    Two headers every browser sends on a top-level navigation and most
    automated clients omit:

    * `Accept-Language` — a browser always states one, even if only
      `en-US`. curl, python-requests, Googlebot and most scrapers send
      none at all. This is the single strongest signal available without
      JavaScript.
    * `Accept` — a browser asks for HTML by name. `*/*`, the curl
      default, is not something a navigating browser sends.

    Deliberately NOT used: the `Sec-Fetch-*` headers. They are a cleaner
    signal on a current browser and absent on every older one, and this
    audience is on phones that are not all current. Missing them would
    mark real readers as crawlers, and under-counting West African
    visitors is the one failure this must not have.
    """
    if not accept_language.strip():
        return True
    return "text/html" not in accept.lower()


def should_record(path: str, method: str, status: int, content_type: str, is_htmx: bool) -> bool:
    """Only full-page HTML GETs that succeeded.

    HTMX fragments are interactions inside a page already counted, and
    counting them would make a page that auto-refreshes look like the
    most-read thing on the site.
    """
    if method != "GET" or is_htmx:
        return False
    if not (200 <= status < 300):
        return False
    if not content_type.startswith("text/html"):
        return False
    return not (path in _SKIP_EXACT or path.startswith(_SKIP_PREFIXES))


def referrer_host(referer: str | None) -> str | None:
    """Host only, never the path — a referring URL can carry search terms
    or a private path. Same-origin referrers are dropped as noise."""
    if not referer:
        return None
    try:
        host = urlsplit(referer).netloc.lower()
    except ValueError:
        return None
    if not host:
        return None
    host = host.split("@")[-1]              # strip any userinfo
    own = urlsplit(settings.public_base_url or "").netloc.lower()
    if own and host == own:
        return None
    return host[:120]


def record(
    *,
    path: str,
    status: int,
    ip: str,
    user_agent: str,
    referer: str | None,
    accept: str = "",
    accept_language: str = "",
    locale: str | None,
    signed_in: bool,
    plan: str | None,
    is_pwa: bool,
) -> bool:
    """Write one pageview. Returns whether a row was written.

    Never raises: analytics must not be able to fail a page render.
    """
    try:
        if is_bot(user_agent):
            return False
        suspected = looks_automated(accept, accept_language)
        day = _today()
        with connect(_db_path()) as conn:
            salt = _salt_for(conn, day)
            repo.insert_view(
                conn,
                ts_utc=utc_iso(),
                day=day,
                path=path[:200],
                status=status,
                referrer_host=referrer_host(referer),
                visitor_hash=_visitor_hash(salt, ip, user_agent, day),
                locale=locale,
                signed_in=signed_in,
                plan=plan,
                is_pwa=is_pwa,
                is_suspected_bot=suspected,
            )
        return True
    except Exception as e:  # pragma: no cover - defensive
        log.warning("analytics: pageview not recorded: %s", type(e).__name__)
        return False


def prune(retain_days: int | None = None) -> int:
    """Drop pageviews older than the retention window."""
    days = retain_days if retain_days is not None else settings.analytics_retain_days
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    with connect(_db_path()) as conn:
        removed = repo.prune(conn, cutoff)
    if removed:
        log.info("analytics: pruned %d pageviews before %s", removed, cutoff)
    return removed


# ---------------------------------------------------------------------------
# Reads — all aggregate
# ---------------------------------------------------------------------------


@dataclass
class Summary:
    since_day: str
    days: list[dict]
    paths: list[dict]
    referrers: list[dict]
    locales: list[dict]
    visitors: int
    views: int
    pricing_visitors: int
    signup_visitors: int
    # What the figures above deliberately leave out, so the exclusion is
    # a number on the page rather than an invisible decision.
    suspected_views: int = 0
    suspected_visitors: int = 0


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


def summary(days: int = 14) -> Summary:
    since = (datetime.now(UTC) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    with connect(_db_path()) as conn:
        daily = _rows(repo.daily_totals(conn, since))
        suspected = repo.suspected_totals(conn, since)
        return Summary(
            since_day=since,
            days=daily,
            paths=_rows(repo.top_paths(conn, since)),
            referrers=_rows(repo.top_referrers(conn, since)),
            locales=_rows(repo.locale_split(conn, since)),
            visitors=repo.total_visitors(conn, since),
            views=sum(d["views"] for d in daily),
            pricing_visitors=repo.visitors_on_path(conn, since, "/pricing"),
            signup_visitors=repo.visitors_on_path(conn, since, "/login"),
            suspected_views=suspected[0],
            suspected_visitors=suspected[1],
        )
