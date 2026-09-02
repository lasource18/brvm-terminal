"""Phase 3c: news service read side + news/CA tabs + /news + overview strip."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from kodji.clock import utcnow
from kodji.config import reset_settings_cache
from kodji.db import connect
from kodji.models import CorporateAction, NewsItem
from kodji.sources._dedupe import news_hash
from kodji.store import news as news_repo

from .conftest import apply_migrations


def _mk_news(url: str, title: str, **extra) -> NewsItem:
    return NewsItem(
        source="sikafinance",
        kind=extra.pop("kind", "news"),
        url=url,
        url_hash=news_hash(url, title),
        title=title,
        **extra,
    )


# ------------------------------------------------------------------ services --

def _fresh_svc(tmp_path, monkeypatch):
    db_path = tmp_path / "kodji.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from kodji.services import news as svc
    return svc, db_path


def test_list_feed_filters_and_pagination(monkeypatch, tmp_path):
    svc, db_path = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        # 30 rows so pagination has something to page over.
        items = [_mk_news(f"https://x/{i}", f"item {i}",
                          published_at=f"2026-08-{(i % 20) + 1:02d}T09:00:00Z")
                 for i in range(30)]
        news_repo.upsert_news_items(conn, items)
        # Tag half of them with category=earnings, relevance=8.
        ids = [r["id"] for r in news_repo.list_news(conn, limit=100)]
        for i in ids[:15]:
            news_repo.apply_tags(
                conn, i, tickers=["SNTS"], relevance=8, category="earnings",
                summary_en="Summary EN.", summary_fr="Résumé FR.",
            )

    # Default page (25 items) + total count.
    feed = svc.list_feed(limit=25)
    assert feed.total == 30
    assert len(feed.items) == 25
    assert feed.page == 1 and feed.total_pages == 2
    assert feed.has_more is True

    # Next page.
    feed2 = svc.list_feed(limit=25, offset=25)
    assert len(feed2.items) == 5
    assert feed2.has_more is False

    # Category filter.
    only_earn = svc.list_feed(category="earnings", limit=100)
    assert only_earn.total == 15
    assert all(r.category == "earnings" for r in only_earn.items)

    # Ticker filter picks up the LLM tag.
    by_ticker = svc.list_feed(ticker="snts", limit=100)  # case-insensitive
    assert by_ticker.total == 15

    # Bogus category is silently ignored (stale bookmarks stay usable).
    bogus = svc.list_feed(category="not-a-thing", limit=5)
    assert bogus.total == 30

    # NewsRow merges ticker_hint + tickers_llm and exposes summaries.
    row = only_earn.items[0]
    assert "SNTS" in row.tickers
    assert row.summary_en == "Summary EN."
    assert row.is_tagged


def test_list_upcoming_actions_joins_security_name(monkeypatch, tmp_path):
    svc, db_path = _fresh_svc(tmp_path, monkeypatch)
    from kodji.models import Security
    from kodji.store import securities as sec_repo

    today = date(2026, 8, 21)
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SGBC", name="SGBCI", kind="equity", country="CI"),
            Security(ticker="TTLC", name="TOTAL CI", kind="equity", country="CI"),
        ])
        news_repo.upsert_corporate_actions(conn, [
            CorporateAction(ticker="SGBC", kind="dividend",
                            ex_date=today + timedelta(days=3),
                            amount=2606.0, source="sikafinance"),
            CorporateAction(ticker="TTLC", kind="dividend",
                            ex_date=today + timedelta(days=15),
                            amount=158.83, source="sikafinance"),
            # Out of window.
            CorporateAction(ticker="SGBC", kind="dividend",
                            ex_date=today + timedelta(days=60),
                            amount=100.0, source="sikafinance"),
        ])

    rows = svc.list_upcoming_actions(days=30, today=today)
    tickers = [r.ticker for r in rows]
    assert "SGBC" in tickers and "TTLC" in tickers
    assert all(r.ex_date is None or r.ex_date <= today + timedelta(days=30) for r in rows)
    name_by_t = {r.ticker: r.name for r in rows}
    assert name_by_t["SGBC"] == "SGBCI"
    assert name_by_t["TTLC"] == "TOTAL CI"

    per_ticker = svc.list_upcoming_actions(ticker="sgbc", days=90, today=today)
    assert {r.ticker for r in per_ticker} == {"SGBC"}


# ------------------------------------------------------------------ web layer -

# The seeded corporate action must stay in the future: the views under
# test filter to *upcoming* actions in a 30-day window, so a hardcoded
# ex_date silently turns these tests red once it slips into the past.
SEEDED_EX_DATE = utcnow().date() + timedelta(days=7)


def _seed_feed(client, *, n=5):
    """Insert `n` tagged news rows and a corporate action against the DB
    the `client` fixture set up. Item `i` gets a distinct `published_at`
    that makes `News {n-1}` newest and `News 0` oldest — so `limit`-based
    pagination is deterministic.
    """
    from kodji.config import settings

    db_path = Path(settings.db_path)
    items = [
        _mk_news(
            f"https://x/n{i}",
            f"News {i} about SNTS",
            # Zero-padded ISO days so string ORDER BY matches chronology.
            # 2026 has enough spare days that n up to ~90 stays valid.
            published_at=f"2026-06-{(i % 28) + 1:02d}T09:00:00Z" if i < 28
            else f"2026-07-{(i - 28) + 1:02d}T09:00:00Z",
        )
        for i in range(n)
    ]
    with connect(db_path) as conn:
        news_repo.upsert_news_items(conn, items)
        ids = [r["id"] for r in news_repo.list_news(conn, limit=100)]
        for i in ids:
            news_repo.apply_tags(
                conn, i, tickers=["SNTS"], relevance=7, category="earnings",
                summary_en="A tagged summary.", summary_fr="Un résumé.",
            )
        news_repo.upsert_corporate_actions(conn, [
            CorporateAction(ticker="SNTS", kind="dividend",
                            ex_date=SEEDED_EX_DATE,
                            amount=1750.0, currency="XOF", yield_pct=5.4,
                            source="sikafinance"),
        ])


def test_news_tab_renders_with_summary_and_ticker_link(client):
    _seed_feed(client, n=3)
    r = client.get("/s/SNTS/news")
    assert r.status_code == 200
    body = r.text
    assert "News · SNTS" in body
    assert "A tagged summary." in body
    assert "earnings" in body
    # Ticker chip renders as a link into the /s/SNTS/news view.
    assert 'href="/s/SNTS/news"' in body
    # "Open in full news view" bridges the tab to the global page.
    assert "/news?ticker=SNTS" in body


def test_corporate_actions_tab_lists_upcoming(client):
    _seed_feed(client)
    r = client.get("/s/SNTS/corporate-actions")
    assert r.status_code == 200
    body = r.text
    assert "Corporate actions · SNTS" in body
    assert SEEDED_EX_DATE.isoformat() in body
    assert "dividend" in body


def test_corporate_actions_tab_hidden_for_index(client):
    r = client.get("/s/BRVMC/corporate-actions")
    assert r.status_code == 404


def test_news_page_renders_filters_and_feed(client):
    _seed_feed(client, n=6)
    r = client.get("/news")
    assert r.status_code == 200
    body = r.text
    assert "All categories" in body
    assert 'name="ticker"' in body
    assert "A tagged summary." in body
    # Category options rendered from the CATEGORIES tuple.
    for cat in ("earnings", "dividend", "governance", "macro", "capital_action", "other"):
        assert f'value="{cat}"' in body


def test_news_page_ticker_filter_narrows(client):
    _seed_feed(client, n=3)
    r = client.get("/news?ticker=SNTS")
    assert r.status_code == 200
    assert "News 0 about SNTS" in r.text
    # An unknown ticker returns zero hits with a helpful empty state.
    r2 = client.get("/news?ticker=ZZZZ")
    assert "No news matches these filters." in r2.text


def test_news_fragment_paginates(client):
    _seed_feed(client, n=30)
    r = client.get("/_frag/news?limit=25&offset=0")
    assert r.status_code == 200
    # Fragment payload contains items but NOT the base template chrome.
    assert "<html" not in r.text
    # Newest item (highest i, thus latest published_at) is on page 1.
    assert "News 29 about SNTS" in r.text
    # Pagination footer shows the correct window.
    assert "page 1/2" in r.text
    # "Next" button hits the fragment endpoint with offset=25.
    assert "offset=25" in r.text

    # Page 2: 5 remaining items, no more "Next", and the oldest is here.
    r2 = client.get("/_frag/news?limit=25&offset=25")
    assert "News 0 about SNTS" in r2.text
    assert "page 2/2" in r2.text
    assert "Next" not in r2.text


def test_news_page_form_has_hx_push_url(client):
    """Phase 8h: filtered views should be shareable via the URL bar."""
    _seed_feed(client, n=3)
    r = client.get("/news")
    assert r.status_code == 200
    assert 'hx-push-url="true"' in r.text


def test_news_fragment_pushes_canonical_url_from_news_page(client):
    """When the filter form fires from `/news`, the fragment endpoint
    responds with an `HX-Push-Url` header pointing back at the canonical
    `/news?...` URL so a reload / paste lands on the same filtered view."""
    _seed_feed(client, n=3)
    r = client.get(
        "/_frag/news?ticker=SNTS&min_relevance=5",
        headers={"HX-Current-URL": "http://testserver/news"},
    )
    assert r.status_code == 200
    push = r.headers.get("hx-push-url")
    assert push is not None
    # Order-agnostic on the query string, but both params must appear.
    assert push.startswith("/news?")
    assert "ticker=SNTS" in push
    assert "min_relevance=5" in push


def test_news_fragment_no_push_url_from_ticker_tab(client):
    """The per-ticker News tab (`/s/{ticker}/news`) uses the same
    fragment endpoint. It must not push a URL — the ticker-scoped path
    is what the user is browsing and should stay intact."""
    _seed_feed(client, n=3)
    r = client.get(
        "/_frag/news?ticker=SNTS",
        headers={"HX-Current-URL": "http://testserver/s/SNTS/news"},
    )
    assert r.status_code == 200
    assert "hx-push-url" not in r.headers


def test_news_fragment_pushes_bare_news_when_no_filters(client):
    """An empty filter set from `/news` pushes `/news` (no `?`), so a
    "reset to unfiltered" click lands the URL on the canonical bare
    view rather than `/news?ticker=&category=&…`."""
    _seed_feed(client, n=3)
    r = client.get(
        "/_frag/news",
        headers={"HX-Current-URL": "http://testserver/news"},
    )
    assert r.status_code == 200
    assert r.headers.get("hx-push-url") == "/news"


def test_overview_shows_corporate_calendar_strip(client):
    _seed_feed(client)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Calendar · next 30d" in body
    assert 'href="/s/SNTS/corporate-actions"' in body


def test_topbar_has_news_link(client):
    r = client.get("/")
    assert '<a href="/news">News</a>' in r.text
