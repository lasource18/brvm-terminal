"""News + corporate-actions ingest **and** read service.

Wraps sikafinance parsers behind a single `poll_all()` entry point so the
scheduler and the `just news-poll` demo can share the same call path.
Ticker resolution here is best-effort (exact name match against
`securities`). The Phase 3b LLM tagger fills in richer attribution on
`news_items.tickers_llm`; anything we resolve now populates
`ticker_hint` as a fast pre-filter for the per-ticker news tab.

Phase 3c added the read side (`list_feed`, `list_upcoming_actions`) so
the tab / page / fragment routes never touch SQL directly.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

import httpx

from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.models import CorporateAction, NewsItem
from brvm.sources import sikafinance
from brvm.sources._http import make_client
from brvm.store import news as news_repo

from ._view import CorporateActionRow, NewsFeed, NewsRow

log = get(__name__)

# Canonical category set — matches the LLM prompt in services/llm.py and
# the CHECK-style expectations of the tagger. Kept here so the /news
# filter dropdown and the tag validator can't drift.
CATEGORIES: tuple[str, ...] = (
    "earnings", "dividend", "governance", "macro", "capital_action", "other",
)

_WS_RE = re.compile(r"\s+")


def _normalize_name(s: str) -> str:
    # Collapse whitespace (incl. nbsp) and uppercase; punctuation-tolerant match.
    return _WS_RE.sub(" ", s.replace("\xa0", " ")).strip().upper()


def _load_name_index(conn: sqlite3.Connection) -> dict[str, str]:
    idx: dict[str, str] = {}
    for r in conn.execute("SELECT ticker, name FROM securities WHERE kind='equity'"):
        idx.setdefault(_normalize_name(r["name"]), r["ticker"])
    return idx


def _resolve_ticker(name_idx: dict[str, str], issuer_name: str | None) -> str | None:
    if not issuer_name:
        return None
    key = _normalize_name(issuer_name)
    hit = name_idx.get(key)
    if hit:
        return hit
    # Fall back to a "starts-with" scan for cases like
    # "SGBCI : SOCIETE GENERALE CI ..." where the trailing details differ.
    for full, tk in name_idx.items():
        if full.startswith(key) or key.startswith(full):
            return tk
    return None


def poll_all(client: httpx.Client | None = None) -> dict[str, int]:
    """Fetch news + communiqués + dividends and persist. Returns row counts."""
    close = client is None
    client = client or make_client()
    try:
        news = sikafinance.fetch_news_feed(client=client)
        communiques = sikafinance.fetch_communiques(client=client)
        dividends = sikafinance.fetch_dividendes(client=client)
    finally:
        if close:
            client.close()

    return _persist(news, communiques, dividends)


def _persist(
    news: list[NewsItem],
    communiques: list[NewsItem],
    dividends: list[CorporateAction],
) -> dict[str, int]:
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        name_idx = _load_name_index(conn)
        known_tickers = {
            r[0] for r in conn.execute("SELECT ticker FROM securities").fetchall()
        }

        # Best-effort ticker resolution on communiqués before insert.
        for c in communiques:
            if c.ticker_hint is None:
                c.ticker_hint = _resolve_ticker(name_idx, c.issuer_name)

        # Drop corporate actions for tickers we haven't ingested yet
        # (FK would fail). Log so the mismatch is visible; typically the
        # next `just snapshot` run picks the missing ticker up.
        known_dividends = [d for d in dividends if d.ticker in known_tickers]
        n_div_skipped = len(dividends) - len(known_dividends)
        if n_div_skipped:
            missing = sorted({d.ticker for d in dividends if d.ticker not in known_tickers})
            log.warning("skipped %d dividend rows for unknown tickers: %s", n_div_skipped, missing)

        n_news_in, n_news_dupe = news_repo.upsert_news_items(conn, news)
        n_comm_in, n_comm_dupe = news_repo.upsert_news_items(conn, communiques)
        n_div_in, n_div_up = news_repo.upsert_corporate_actions(conn, known_dividends)

    counts = {
        "news_in": n_news_in,
        "news_dupe": n_news_dupe,
        "communiques_in": n_comm_in,
        "communiques_dupe": n_comm_dupe,
        "dividends_in": n_div_in,
        "dividends_updated": n_div_up,
        "dividends_skipped_unknown_ticker": n_div_skipped,
    }
    log.info("news poll: %s", counts)
    return counts


# --- read side (Phase 3c) --------------------------------------------------

def _db_path() -> Path:
    return Path(settings.db_path)


def _row_to_news(r: sqlite3.Row) -> NewsRow:
    # Merge ticker_hint + tickers_llm into one deduped display list. The
    # LLM CSV is normalized on write (uppercase, comma-separated), so a
    # plain split is safe.
    tickers: list[str] = []
    if r["ticker_hint"]:
        tickers.append(r["ticker_hint"])
    if r["tickers_llm"]:
        for t in r["tickers_llm"].split(","):
            t = t.strip()
            if t and t not in tickers:
                tickers.append(t)
    return NewsRow(
        id=r["id"],
        source=r["source"],
        kind=r["kind"],
        url=r["url"],
        title=r["title"],
        chapeau=r["chapeau"],
        issuer_name=r["issuer_name"],
        tickers=tickers,
        relevance=r["relevance"],
        category=r["category_llm"],
        summary_fr=r["summary_fr"],
        summary_en=r["summary_en"],
        published_at=r["published_at"],
        fetched_utc=r["fetched_utc"],
    )


def list_feed_from_rows(rows: list, *, limit: int = 25) -> NewsFeed:
    """Wrap a caller-provided row list in the standard NewsFeed shape.

    Used by the bond News tab (`services.bonds.list_issuer_news`) which
    matches on `issuer_name` substring rather than the ticker/relevance
    machinery that `list_feed` gates on. The `total` matches the row
    count because there's no separate count query — this is a
    small-result fallback, not a paginated feed.
    """
    items = [_row_to_news(r) for r in rows]
    return NewsFeed(
        items=items,
        total=len(items),
        limit=limit,
        offset=0,
        filters={
            "ticker": "", "category": "", "date_from": "",
            "date_to": "", "min_relevance": "",
        },
    )


def list_feed(
    *,
    ticker: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_relevance: int | None = None,
    limit: int = 25,
    offset: int = 0,
) -> NewsFeed:
    """Paginated news feed. All filters are optional; None means no filter."""
    ticker = ticker.upper() if ticker else None
    if category and category not in CATEGORIES:
        category = None  # ignore unknown values quietly so a stale bookmark still renders
    with connect(_db_path()) as conn:
        rows = news_repo.list_news(
            conn,
            ticker=ticker,
            category=category,
            date_from=date_from,
            date_to=date_to,
            min_relevance=min_relevance,
            limit=limit,
            offset=offset,
        )
        total = news_repo.count_news(
            conn,
            ticker=ticker,
            category=category,
            date_from=date_from,
            date_to=date_to,
            min_relevance=min_relevance,
        )
    return NewsFeed(
        items=[_row_to_news(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        filters={
            "ticker": ticker or "",
            "category": category or "",
            "date_from": date_from or "",
            "date_to": date_to or "",
            "min_relevance": "" if min_relevance is None else str(min_relevance),
        },
    )


def _row_to_action(r: sqlite3.Row) -> CorporateActionRow:
    ex = r["ex_date"]
    pay = r["pay_date"]
    return CorporateActionRow(
        id=r["id"],
        ticker=r["ticker"],
        name=None,  # populated by list_upcoming_actions after a name lookup

        kind=r["kind"],
        ex_date=date.fromisoformat(ex) if ex else None,
        pay_date=date.fromisoformat(pay) if pay else None,
        amount=r["amount"],
        currency=r["currency"],
        yield_pct=r["yield_pct"],
        note=r["note"],
        source=r["source"],
        source_url=r["source_url"],
    )


def list_upcoming_actions(
    *,
    ticker: str | None = None,
    days: int = 30,
    today: date | None = None,
) -> list[CorporateActionRow]:
    """Upcoming corporate actions joined with `securities.name` for display."""
    ticker = ticker.upper() if ticker else None
    today = today or date.today()
    # Reuse the store's window helper for consistency, then join names in
    # a second pass — the alternative (a JOIN in the store layer) would
    # force every caller through the same shape.
    with connect(_db_path()) as conn:
        raw = news_repo.list_corporate_actions_upcoming(
            conn, ticker=ticker, days=days, today=today
        )
        if not raw:
            return []
        tickers = tuple({r["ticker"] for r in raw})
        placeholders = ",".join("?" * len(tickers))
        name_by_ticker = {
            r[0]: r[1]
            for r in conn.execute(
                f"SELECT ticker, name FROM securities WHERE ticker IN ({placeholders})",
                tickers,
            ).fetchall()
        }
    out: list[CorporateActionRow] = []
    for r in raw:
        row = _row_to_action(r)
        row.name = name_by_ticker.get(row.ticker)
        out.append(row)
    return out
