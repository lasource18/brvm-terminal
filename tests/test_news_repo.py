"""Tests for store/news.py: dedupe on url_hash + corporate_actions upsert."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from brvm.db import connect
from brvm.models import CorporateAction, NewsItem, Security
from brvm.sources._dedupe import news_hash
from brvm.store import news as news_repo
from brvm.store import securities as sec_repo

from .conftest import apply_migrations


def _init(tmp_db_path: Path) -> None:
    with connect(tmp_db_path) as conn:
        apply_migrations(conn)


def _mk_news(url: str, title: str, **extra) -> NewsItem:
    return NewsItem(
        source="sikafinance",
        kind=extra.pop("kind", "news"),
        url=url,
        url_hash=news_hash(url, title),
        title=title,
        **extra,
    )


def test_news_dedupe_on_url_hash(tmp_db_path: Path):
    _init(tmp_db_path)
    a = _mk_news("https://x/a", "First title")
    b = _mk_news("https://x/b", "Second title")
    with connect(tmp_db_path) as conn:
        ins, dupe = news_repo.upsert_news_items(conn, [a, b])
        assert (ins, dupe) == (2, 0)
        # Re-insert the exact same rows -> both dupes, no new rows.
        ins2, dupe2 = news_repo.upsert_news_items(conn, [a, b])
        assert (ins2, dupe2) == (0, 2)
        assert news_repo.count_news(conn) == 2


def test_news_dedupe_ignores_url_case_trailing_slash(tmp_db_path: Path):
    _init(tmp_db_path)
    a = _mk_news("https://X.com/Foo/", "Same title")
    b = _mk_news("https://x.com/foo", "Same title")
    with connect(tmp_db_path) as conn:
        # Same normalized url + title -> same hash -> second insert is a dupe.
        assert a.url_hash == b.url_hash
        ins, dupe = news_repo.upsert_news_items(conn, [a, b])
        assert (ins, dupe) == (1, 1)


def test_corporate_actions_upsert(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        sec_repo.upsert(
            conn,
            [
                Security(ticker="SGBC", name="SGBCI", kind="equity", country="CI"),
                Security(ticker="TTLC", name="TOTAL CI", kind="equity", country="CI"),
            ],
        )
        first = [
            CorporateAction(
                ticker="SGBC", kind="dividend", ex_date=date(2026, 8, 21),
                amount=2606.0, currency="XOF", yield_pct=6.52,
                source="sikafinance",
            ),
            CorporateAction(
                ticker="TTLC", kind="dividend", ex_date=date(2026, 8, 28),
                amount=158.83, currency="XOF", yield_pct=4.81,
                source="sikafinance",
            ),
        ]
        ins, upd = news_repo.upsert_corporate_actions(conn, first)
        assert (ins, upd) == (2, 0)

        # Refresh SGBC amount; ex_date + kind + ticker key unchanged -> update.
        second = [
            CorporateAction(
                ticker="SGBC", kind="dividend", ex_date=date(2026, 8, 21),
                amount=2650.0, currency="XOF", yield_pct=6.60,
                source="sikafinance",
            ),
        ]
        ins2, upd2 = news_repo.upsert_corporate_actions(conn, second)
        assert (ins2, upd2) == (0, 1)
        row = conn.execute(
            "SELECT amount, yield_pct FROM corporate_actions "
            "WHERE ticker='SGBC' AND kind='dividend' AND ex_date='2026-08-21'"
        ).fetchone()
        assert row["amount"] == 2650.0
        assert row["yield_pct"] == 6.60


def test_upcoming_window_and_ticker_filter(tmp_db_path: Path):
    _init(tmp_db_path)
    today = date(2026, 8, 20)
    with connect(tmp_db_path) as conn:
        sec_repo.upsert(
            conn,
            [
                Security(ticker="SGBC", name="SGBCI", kind="equity", country="CI"),
                Security(ticker="SPHC", name="SAPH CI", kind="equity", country="CI"),
            ],
        )
        news_repo.upsert_corporate_actions(
            conn,
            [
                # In-window ex-date.
                CorporateAction(ticker="SGBC", kind="dividend",
                                ex_date=today + timedelta(days=1),
                                amount=2606.0, source="sikafinance"),
                # Beyond 30-day window.
                CorporateAction(ticker="SGBC", kind="dividend",
                                ex_date=today + timedelta(days=60),
                                amount=100.0, source="sikafinance"),
                # TBD (ex_date NULL) — should still appear.
                CorporateAction(ticker="SPHC", kind="dividend",
                                ex_date=None, amount=489.0,
                                note="A préciser", source="sikafinance"),
            ],
        )
        all_up = news_repo.list_corporate_actions_upcoming(conn, days=30, today=today)
        # In-window ex-date + TBD row; the +60d row is excluded.
        tickers_kinds = {(r["ticker"], r["kind"], r["ex_date"]) for r in all_up}
        assert ("SGBC", "dividend", (today + timedelta(days=1)).isoformat()) in tickers_kinds
        assert ("SPHC", "dividend", None) in tickers_kinds
        assert all(
            r["ex_date"] is None
            or r["ex_date"] <= (today + timedelta(days=30)).isoformat()
            for r in all_up
        )

        only_sgbc = news_repo.list_corporate_actions_upcoming(
            conn, ticker="SGBC", days=30, today=today
        )
        assert {r["ticker"] for r in only_sgbc} == {"SGBC"}


def test_list_news_ticker_filter_matches_hint_and_llm(tmp_db_path: Path):
    _init(tmp_db_path)
    a = _mk_news("https://x/a", "SNTS earnings", ticker_hint="SNTS")
    b = _mk_news("https://x/b", "unrelated")
    with connect(tmp_db_path) as conn:
        news_repo.upsert_news_items(conn, [a, b])
        # Simulate the 3b tagger writing tickers_llm CSV on row b.
        conn.execute(
            "UPDATE news_items SET tickers_llm='SNTS,ORAC' WHERE url_hash=?",
            (b.url_hash,),
        )
        conn.commit()
        got = news_repo.list_news(conn, ticker="SNTS")
        titles = {r["title"] for r in got}
        assert titles == {"SNTS earnings", "unrelated"}

        none = news_repo.list_news(conn, ticker="NOPE")
        assert none == []


def test_list_untagged_only_returns_unstamped_rows(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        news_repo.upsert_news_items(
            conn, [_mk_news("https://x/1", "One"), _mk_news("https://x/2", "Two")]
        )
        assert news_repo.count_untagged(conn) == 2

        first_id = news_repo.list_untagged(conn)[0]["id"]
        news_repo.apply_tags(conn, first_id, tickers=["SNTS"], relevance=6, category="earnings")

        left = news_repo.list_untagged(conn)
        assert news_repo.count_untagged(conn) == 1
        assert first_id not in {r["id"] for r in left}


def test_apply_tags_writes_csv_that_the_ticker_filter_matches(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        news_repo.upsert_news_items(conn, [_mk_news("https://x/1", "Sonatel et Orange")])
        item_id = news_repo.list_untagged(conn)[0]["id"]
        news_repo.apply_tags(
            conn,
            item_id,
            tickers=["snts", "ORAC", "SNTS"],  # normalized + deduped on write
            relevance=8,
            category="earnings",
            summary_fr="Résumé.",
            summary_en="Summary.",
        )
        row = news_repo.list_news(conn)[0]
        assert row["tickers_llm"] == "SNTS,ORAC"
        assert row["relevance"] == 8
        assert row["tagged_utc"]
        assert len(news_repo.list_news(conn, ticker="ORAC")) == 1
        assert len(news_repo.list_news(conn, ticker="CFAC")) == 0


def test_apply_tags_with_no_tickers_stores_null(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        news_repo.upsert_news_items(conn, [_mk_news("https://x/1", "BCEAO")])
        item_id = news_repo.list_untagged(conn)[0]["id"]
        news_repo.apply_tags(conn, item_id, tickers=[], relevance=4, category="macro")
        row = news_repo.list_news(conn)[0]
        assert row["tickers_llm"] is None
        # ...and an empty CSV must not make the LIKE filter match everything.
        assert news_repo.list_news(conn, ticker="SNTS") == []


def test_ingest_never_clobbers_existing_tags(tmp_db_path: Path):
    """Re-polling the same article must not wipe what the tagger wrote."""
    _init(tmp_db_path)
    item = _mk_news("https://x/1", "Sonatel")
    with connect(tmp_db_path) as conn:
        news_repo.upsert_news_items(conn, [item])
        item_id = news_repo.list_untagged(conn)[0]["id"]
        news_repo.apply_tags(conn, item_id, tickers=["SNTS"], relevance=9, category="earnings")

        ins, dupe = news_repo.upsert_news_items(conn, [item])
        assert (ins, dupe) == (0, 1)
        row = news_repo.list_news(conn)[0]
        assert row["tickers_llm"] == "SNTS" and row["relevance"] == 9
