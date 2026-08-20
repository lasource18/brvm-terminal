"""News + corporate-actions ingest service.

Wraps sikafinance parsers behind a single `poll_all()` entry point so the
scheduler and the `just news-poll` demo can share the same call path.
Ticker resolution here is best-effort (exact name match against
`securities`). The Phase 3b LLM tagger fills in richer attribution on
`news_items.tickers_llm`; anything we resolve now populates
`ticker_hint` as a fast pre-filter for the per-ticker news tab.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import httpx

from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.models import CorporateAction, NewsItem
from brvm.sources import sikafinance
from brvm.sources._http import make_client
from brvm.store import news as news_repo

log = get(__name__)

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
