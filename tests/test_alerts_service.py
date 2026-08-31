"""Phase 6a: alerts store + service.

Covers:

* Rule CRUD + enabled toggle.
* Price-move evaluator: fires once per (rule, snapshot), watchlist-wide
  vs per-ticker rules, |change_pct| gate, missing threshold skip.
* New-filing evaluator: watched tickers, doc_types filter, dedupe on
  re-run.
* News evaluator: only tagged rows (relevance not NULL), min_relevance
  gate, ticker attribution via ticker_hint OR tickers_llm CSV.
* Delivery: happy path, http failure, no-webhook skip, batch cap.

Uses a scripted Discord sender so no real network is touched. The
`_setup` helper points settings at a tmp DB via the lazy proxy — no
importlib reloads needed (see Phase 6a's settings refactor).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx

from kodji.config import reset_settings_cache
from kodji.db import connect
from kodji.models import (
    AlertEvent,
    AlertRule,
    Filing,
    NewsItem,
    Quote,
    Security,
)
from kodji.services.accounts import DEFAULT_ACCOUNT_ID
from kodji.sources._dedupe import news_hash
from kodji.store import alerts as alerts_repo
from kodji.store import filings as filings_repo
from kodji.store import news as news_repo
from kodji.store import quotes as quotes_repo
from kodji.store import securities as sec_repo

from .conftest import apply_migrations


def _setup(monkeypatch, tmp_path: Path):
    """Fresh DB + a handful of securities. Returns (db_path, alerts_svc)."""
    db_path = tmp_path / "kodji.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")  # off unless a test flips it
    reset_settings_cache()
    from kodji.services import alerts as svc

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
            Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
            Security(ticker="SPHC", name="SAPH CI", kind="equity", country="CI"),
        ])
    return db_path, svc


def _seed_snap(db_path: Path, ticker: str, change_pct: float, last: float = 10_000) -> None:
    with connect(db_path) as conn:
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker=ticker, source="sikafinance", last=last, change_pct=change_pct,
                  volume=100, turnover=last * 100),
        ])


# --- store CRUD ------------------------------------------------------------


def test_create_rule_and_list(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    rule_id = svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(
        kind="price_move", ticker="SNTS", threshold_pct=5.0, label="big"
    ))
    assert rule_id > 0
    rules = svc.list_rules(DEFAULT_ACCOUNT_ID)
    assert len(rules) == 1
    assert rules[0].label == "big"
    assert rules[0].enabled is True


def test_toggle_and_delete_rule(monkeypatch, tmp_path):
    _db_path, svc = _setup(monkeypatch, tmp_path)
    rid = svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker="SNTS", threshold_pct=1.0))
    assert svc.set_enabled(DEFAULT_ACCOUNT_ID, rid, False) == 1
    assert svc.list_rules(DEFAULT_ACCOUNT_ID)[0].enabled is False
    assert svc.list_rules(DEFAULT_ACCOUNT_ID, enabled_only=True) == []
    assert svc.delete_rule(DEFAULT_ACCOUNT_ID, rid) == 1
    assert svc.list_rules(DEFAULT_ACCOUNT_ID) == []


def test_record_event_deduplicates_on_key(monkeypatch, tmp_path):
    db_path, _svc = _setup(monkeypatch, tmp_path)
    with connect(db_path) as conn:
        rid = alerts_repo.create_rule(conn, DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", threshold_pct=1.0))
        assert alerts_repo.record_event(
            conn, rule_id=rid, kind="price_move", ticker="SNTS",
            subject="s", body="b", payload=None, dedupe_key="k1",
        ) is not None
        assert alerts_repo.record_event(
            conn, rule_id=rid, kind="price_move", ticker="SNTS",
            subject="s2", body="b2", payload=None, dedupe_key="k1",
        ) is None  # dedupe hit
        # But a different key is a fresh insert.
        assert alerts_repo.record_event(
            conn, rule_id=rid, kind="price_move", ticker="SNTS",
            subject="s3", body="b3", payload=None, dedupe_key="k2",
        ) is not None


# --- evaluators: price moves -----------------------------------------------


def test_price_move_fires_on_ticker_specific_rule(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker="SNTS", threshold_pct=3.0))
    _seed_snap(db_path, "SNTS", 5.0)
    _seed_snap(db_path, "ORAC", 4.0)  # not watched → no fire

    counts = svc.evaluate_all()
    assert counts.price_move_fired == 1
    assert counts.total_deduped == 0
    events = svc.list_recent_events()
    assert len(events) == 1
    assert events[0].ticker == "SNTS"
    assert "+5.00%" in events[0].subject


def test_price_move_wildcard_ticker_scans_every_snapshot(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker=None, threshold_pct=2.0))
    _seed_snap(db_path, "SNTS", 3.0)
    _seed_snap(db_path, "ORAC", -4.0)
    _seed_snap(db_path, "SPHC", 1.0)  # under threshold

    counts = svc.evaluate_all()
    assert counts.price_move_fired == 2
    tickers = {e.ticker for e in svc.list_recent_events()}
    assert tickers == {"SNTS", "ORAC"}


def test_price_move_reeval_is_a_no_op(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker="SNTS", threshold_pct=3.0))
    _seed_snap(db_path, "SNTS", 5.0)

    first = svc.evaluate_all()
    assert first.price_move_fired == 1
    second = svc.evaluate_all()
    assert second.price_move_fired == 0
    assert second.total_deduped == 1


def test_price_move_rule_without_threshold_is_skipped(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker="SNTS", threshold_pct=None))
    _seed_snap(db_path, "SNTS", 10.0)
    counts = svc.evaluate_all()
    assert counts.price_move_fired == 0


def test_disabled_rule_does_not_fire(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    rid = svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker="SNTS", threshold_pct=1.0))
    svc.set_enabled(DEFAULT_ACCOUNT_ID, rid, False)
    _seed_snap(db_path, "SNTS", 5.0)
    counts = svc.evaluate_all()
    assert counts.price_move_fired == 0
    assert counts.rules_considered == 0


# --- evaluators: new filings -----------------------------------------------


def _seed_filing(
    db_path: Path,
    ticker: str = "SNTS",
    doc_type: str = "rapport_annuel",
    period_year: int = 2024,
    url_suffix: str = "a",
) -> int:
    with connect(db_path) as conn:
        filings_repo.upsert_filings(conn, [Filing(
            ticker=ticker,
            issuer_name=ticker,
            doc_type=doc_type,
            period_kind="annual",
            period_year=period_year,
            source="brvm_org",
            source_url=f"https://brvm.org/{ticker}-{url_suffix}.pdf",
            url_hash=f"hash-{ticker}-{url_suffix}",
            published_date=date(period_year + 1, 3, 15),
            file_path=f"data/filings/{ticker}/{url_suffix}.pdf",
            size_bytes=1024,
            sha256=f"deadbeef-{url_suffix}",
            page_count=42,
        )])
        return int(conn.execute(
            "SELECT id FROM filings ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"])


def test_new_filing_fires_for_watched_ticker(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="new_filing", ticker="SNTS"))
    _seed_filing(db_path, ticker="SNTS")
    _seed_filing(db_path, ticker="ORAC", url_suffix="b")

    counts = svc.evaluate_all()
    assert counts.new_filing_fired == 1
    ev = svc.list_recent_events()[0]
    assert ev.ticker == "SNTS"


def test_new_filing_doc_type_filter(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(
        kind="new_filing", ticker=None, doc_types="rapport_annuel"
    ))
    _seed_filing(db_path, doc_type="rapport_annuel")
    _seed_filing(db_path, doc_type="rapport_activites", url_suffix="b")
    counts = svc.evaluate_all()
    assert counts.new_filing_fired == 1
    assert svc.list_recent_events()[0].payload_json is not None


def test_new_filing_rerun_is_deduped(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="new_filing", ticker="SNTS"))
    _seed_filing(db_path)
    assert svc.evaluate_all().new_filing_fired == 1
    assert svc.evaluate_all().new_filing_fired == 0
    assert svc.evaluate_all().total_deduped == 1


def test_new_filing_rule_skips_rows_older_than_rule_created_utc(
    monkeypatch, tmp_path
):
    """F-16: adding a fresh wildcard rule after a real filings pull
    used to fire ~100 events about years-old documents (roughly 50 min
    of Discord spam at the 10-per-5-min delivery cap). Rows whose
    fetched_utc pre-dates the rule must be silently skipped."""
    db_path, svc = _setup(monkeypatch, tmp_path)
    # Seed a historical filing FIRST so its fetched_utc is earlier
    # than the rule's created_utc.
    _seed_filing(db_path, ticker="SNTS", url_suffix="hist")
    # Roll `fetched_utc` back so the historical row is unambiguously
    # older than what create_rule() will stamp on the rule below.
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE filings SET fetched_utc = ? WHERE url_hash = ?",
            ("2020-01-01T00:00:00+00:00", "hash-SNTS-hist"),
        )
        conn.commit()
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="new_filing", ticker=None))
    # New filing arrives after rule creation — should fire.
    _seed_filing(db_path, ticker="ORAC", url_suffix="new")
    counts = svc.evaluate_all()
    assert counts.new_filing_fired == 1
    ev = svc.list_recent_events()[0]
    assert ev.ticker == "ORAC"


def test_news_rule_skips_rows_older_than_rule_created_utc(
    monkeypatch, tmp_path
):
    """F-16 mirror for the news evaluator: a min_relevance=0 news rule
    added late must not replay the tagged historical corpus."""
    db_path, svc = _setup(monkeypatch, tmp_path)
    _seed_news(db_path, title="OLD news",
               tickers_llm="SNTS", relevance=8, category="earnings")
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE news_items SET fetched_utc = ? WHERE title = ?",
            ("2020-01-01T00:00:00+00:00", "OLD news"),
        )
        conn.commit()
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="news", ticker=None, min_relevance=0))
    _seed_news(db_path, title="FRESH news",
               tickers_llm="ORAC", relevance=9, category="dividend")
    counts = svc.evaluate_all()
    assert counts.news_fired == 1
    ev = svc.list_recent_events()[0]
    assert "FRESH news" in ev.subject


# --- evaluators: news ------------------------------------------------------


def _seed_news(db_path: Path, *, title: str, tickers_llm: str,
               relevance: int, ticker_hint: str | None = None,
               category: str = "earnings") -> int:
    url = f"https://x/{title[:20].replace(' ', '_')}"
    with connect(db_path) as conn:
        news_repo.upsert_news_items(conn, [NewsItem(
            source="sikafinance", kind="news", url=url,
            url_hash=news_hash(url, title), title=title,
            ticker_hint=ticker_hint, published_at="2026-08-20T09:00:00Z",
        )])
        row_id = int(conn.execute(
            "SELECT id FROM news_items ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"])
        news_repo.apply_tags(
            conn, row_id,
            tickers=[t for t in tickers_llm.split(",") if t],
            relevance=relevance, category=category,
            summary_en="EN", summary_fr="FR",
        )
        return row_id


def test_news_evaluator_only_matches_tagged_rows(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="news", ticker="SNTS", min_relevance=5))
    _seed_news(db_path, title="SONATEL earnings", tickers_llm="SNTS", relevance=8)
    # An untagged row (relevance IS NULL) must not fire even if the
    # ticker_hint matches — the min_relevance gate has nothing to check.
    url = "https://x/untagged"
    with connect(db_path) as conn:
        news_repo.upsert_news_items(conn, [NewsItem(
            source="sikafinance", kind="news", url=url,
            url_hash=news_hash(url, "untagged"), title="untagged",
            ticker_hint="SNTS", published_at="2026-08-20T09:00:00Z",
        )])

    counts = svc.evaluate_all()
    assert counts.news_fired == 1
    assert svc.list_recent_events()[0].ticker == "SNTS"


def test_news_min_relevance_gate(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="news", ticker=None, min_relevance=7))
    _seed_news(db_path, title="hi rel", tickers_llm="SNTS", relevance=8)
    _seed_news(db_path, title="lo rel", tickers_llm="ORAC", relevance=3)
    counts = svc.evaluate_all()
    assert counts.news_fired == 1


def test_news_matches_via_tickers_llm_csv(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="news", ticker="ORAC", min_relevance=0))
    # ticker_hint is different from the watched ticker — the LLM CSV is
    # the only path to a match.
    _seed_news(
        db_path, title="cross-mentioned", tickers_llm="SNTS,ORAC",
        relevance=6, ticker_hint="SNTS",
    )
    counts = svc.evaluate_all()
    assert counts.news_fired == 1
    assert svc.list_recent_events()[0].ticker == "ORAC"


# --- delivery --------------------------------------------------------------


class _StubSender:
    def __init__(
        self, *, ok: bool = True, permanent: bool = False, note: str | None = None
    ) -> None:
        self.ok = ok
        self.permanent = permanent
        self.note = note
        self.sent: list[AlertEvent] = []
        self.closed = False

    def send(self, event: AlertEvent):
        from kodji.services.alerts import SendResult

        self.sent.append(event)
        if self.ok:
            return SendResult(ok=True, note="ok")
        return SendResult(
            ok=False,
            note=self.note or ("http_400" if self.permanent else "http_500"),
            permanent=self.permanent,
        )

    def close(self) -> None:
        self.closed = True


def _fire_one(db_path: Path, svc) -> int:
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker="SNTS", threshold_pct=1.0))
    _seed_snap(db_path, "SNTS", 5.0)
    svc.evaluate_all()
    return svc.list_recent_events()[0].id or 0


def test_delivery_happy_path_marks_events_ok(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    _fire_one(db_path, svc)
    sender = _StubSender(ok=True)

    counts = svc.deliver_pending(sender=sender)
    assert counts.delivered == 1
    assert counts.failed == 0
    events = svc.list_recent_events()
    assert events[0].delivery_status == "ok"
    assert events[0].delivered_utc is not None


def test_delivery_failure_leaves_events_undelivered(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    _fire_one(db_path, svc)
    sender = _StubSender(ok=False)

    counts = svc.deliver_pending(sender=sender)
    assert counts.delivered == 0
    assert counts.failed == 1
    events = svc.list_recent_events()
    assert events[0].delivered_utc is None  # queued for the next pass
    assert events[0].delivery_status == "failed"


def test_delivery_no_webhook_skips_events(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    _fire_one(db_path, svc)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    reset_settings_cache()

    counts = svc.deliver_pending()
    assert counts.skipped == 1
    assert counts.reason == "no_webhook"
    events = svc.list_recent_events()
    assert events[0].delivery_status == "skipped"
    assert events[0].delivered_utc is not None


def test_delivery_batch_cap_limits_a_single_pass(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path)
    # Fire three events by seeding three tickers with a wildcard rule.
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker=None, threshold_pct=1.0))
    _seed_snap(db_path, "SNTS", 5.0)
    _seed_snap(db_path, "ORAC", 4.0)
    _seed_snap(db_path, "SPHC", 3.0)
    svc.evaluate_all()

    sender = _StubSender(ok=True)
    counts = svc.deliver_pending(sender=sender, limit=2)
    assert counts.delivered == 2
    # One event still queued for the next pass.
    with connect(db_path) as conn:
        remaining = alerts_repo.count_undelivered(conn)
    assert remaining == 1


def test_delivery_stops_on_first_failure(monkeypatch, tmp_path):
    """A webhook that's failing shouldn't be spammed with every queued
    event on the same pass — retry the whole batch next time."""
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker=None, threshold_pct=1.0))
    _seed_snap(db_path, "SNTS", 5.0)
    _seed_snap(db_path, "ORAC", 4.0)
    _seed_snap(db_path, "SPHC", 3.0)
    svc.evaluate_all()

    sender = _StubSender(ok=False)
    counts = svc.deliver_pending(sender=sender)
    assert counts.failed == 1
    assert len(sender.sent) == 1


# --- F-01: hour-bucket / session dedupe -----------------------------------
#
# Prior behaviour: `_price_move_dedupe` keyed on the raw `captured_utc`, so
# every hourly re-scrape of Friday's close over a full weekend counted as a
# fresh snapshot and re-fired the alert ~40 times. The scheduler runs the
# evaluator on every snapshot cycle — these tests drive the scheduled
# entry point (`evaluate_all`) with multiple back-to-back snapshots, the
# way production exercises the code.


def _stamp_snap(db_path: Path, ticker: str, change_pct: float,
                captured_utc: str, last: float = 10_000) -> None:
    """Insert a snapshot with an explicit `captured_utc`. Bypasses
    `insert_snapshots` which always stamps utc_iso()."""
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO quote_snapshots
              (ticker, captured_utc, source, last, change_pct,
               volume, turnover, is_stale)
            VALUES (?, ?, 'sikafinance', ?, ?, 100, ?, 0)
            """,
            (ticker, captured_utc, last, change_pct, last * 100),
        )
        conn.commit()


def test_price_move_dedupes_across_intraday_snapshots(monkeypatch, tmp_path):
    """Two snapshots on the same trading session must only fire once,
    even when the second row lands with a fresh `captured_utc`."""
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker="SNTS", threshold_pct=3.0))

    _stamp_snap(db_path, "SNTS", 4.5, "2026-08-28T09:15:00Z")
    assert svc.evaluate_all().price_move_fired == 1

    _stamp_snap(db_path, "SNTS", 4.5, "2026-08-28T11:15:00Z")
    counts = svc.evaluate_all()
    assert counts.price_move_fired == 0
    assert counts.total_deduped == 1


def test_price_move_dedupes_weekend_rescrapes_of_friday_close(
    monkeypatch, tmp_path
):
    """The bug this test pins: Friday close re-scraped hourly through
    Sat/Sun shouldn't re-fire — all three snapshots collapse to the
    Friday session bucket."""
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker="SNTS", threshold_pct=3.0))

    # Friday close.
    _stamp_snap(db_path, "SNTS", 4.5, "2026-08-28T14:59:00Z")
    assert svc.evaluate_all().price_move_fired == 1

    # Saturday and Sunday re-scrapes carry a fresh timestamp but the
    # underlying data is unchanged.
    _stamp_snap(db_path, "SNTS", 4.5, "2026-08-29T09:00:00Z")
    _stamp_snap(db_path, "SNTS", 4.5, "2026-08-30T09:00:00Z")
    counts = svc.evaluate_all()
    assert counts.price_move_fired == 0
    assert counts.total_deduped == 1  # `_latest_snapshots` picks the newest


def test_price_move_fires_again_on_next_session(monkeypatch, tmp_path):
    """A fresh trading session must be able to fire a new alert."""
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker="SNTS", threshold_pct=3.0))

    _stamp_snap(db_path, "SNTS", 4.5, "2026-08-28T14:59:00Z")
    assert svc.evaluate_all().price_move_fired == 1

    _stamp_snap(db_path, "SNTS", 4.5, "2026-08-31T09:15:00Z")  # Mon
    assert svc.evaluate_all().price_move_fired == 1


# --- F-20: `last=None` guard ----------------------------------------------


def test_price_move_evaluator_survives_null_last(monkeypatch, tmp_path):
    """A partially-parsed snapshot (change_pct known, last still NULL)
    used to raise `TypeError` in the alert subject/body formatter and
    the scheduler swallowed the whole cycle — filings and news
    evaluations silently produced zero events. Now the row still fires
    and downstream evaluators keep running."""
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker="SNTS", threshold_pct=3.0))

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO quote_snapshots
              (ticker, captured_utc, source, last, change_pct, is_stale)
            VALUES ('SNTS', '2026-08-28T14:59:00Z', 'sikafinance',
                    NULL, 4.5, 0)
            """
        )
        conn.commit()

    counts = svc.evaluate_all()
    assert counts.price_move_fired == 1
    ev = svc.list_recent_events()[0]
    assert "—" in ev.subject


# --- F-05: no webhook leak + no queue wedge on permanent 4xx --------------


def test_delivery_result_note_never_contains_webhook_url(
    monkeypatch, tmp_path
):
    """`str(httpx.HTTPError)` used to embed the full webhook URL —
    including the token — into the log line. The sender must never
    return the URL in the note."""
    from kodji.services.alerts import SendResult, _DiscordSender

    _db_path, _svc = _setup(monkeypatch, tmp_path)
    secret_url = "https://discord.com/api/webhooks/12345/SUPER-SECRET-TOKEN"
    sender = _DiscordSender(webhook_url=secret_url)

    def _raise(*_a, **_kw):
        # Emulate httpx.RequestError shape: the URL leaks through str(e).
        raise httpx.ConnectError(f"connect failed for {secret_url}")

    monkeypatch.setattr(sender.client, "post", _raise)
    result: SendResult = sender.send(
        AlertEvent(rule_id=1, kind="price_move", ticker="SNTS",
                   subject="s", body="b", dedupe_key="k")
    )
    assert result.ok is False
    assert "SUPER-SECRET" not in result.note
    assert "discord.com" not in result.note


def test_delivery_permanent_4xx_leaves_the_queue(monkeypatch, tmp_path):
    """A 400 / 401 / 403 will never succeed on retry: leaving the event
    at head-of-queue would wedge everything behind it. The event must be
    stamped delivered_utc so the next event in the batch gets a chance."""
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker=None, threshold_pct=1.0))
    _seed_snap(db_path, "SNTS", 5.0)
    _seed_snap(db_path, "ORAC", 4.0)
    svc.evaluate_all()

    sender = _StubSender(ok=False, permanent=True)
    counts = svc.deliver_pending(sender=sender)
    # Both events attempted (no wedge) and both drained from the queue.
    assert len(sender.sent) == 2
    assert counts.failed == 2
    with connect(db_path) as conn:
        remaining = alerts_repo.count_undelivered(conn)
    assert remaining == 0
    events = svc.list_recent_events()
    assert all(e.delivery_status == "permanent_failure" for e in events)


def test_delivery_transient_5xx_keeps_queue_intact(monkeypatch, tmp_path):
    """Transient failures still stop-and-retry: the queue must stay
    intact for the next pass."""
    db_path, svc = _setup(monkeypatch, tmp_path)
    svc.create_rule(DEFAULT_ACCOUNT_ID, AlertRule(kind="price_move", ticker=None, threshold_pct=1.0))
    _seed_snap(db_path, "SNTS", 5.0)
    _seed_snap(db_path, "ORAC", 4.0)
    svc.evaluate_all()

    sender = _StubSender(ok=False, permanent=False)
    counts = svc.deliver_pending(sender=sender)
    assert len(sender.sent) == 1  # broke after first
    assert counts.failed == 1
    with connect(db_path) as conn:
        remaining = alerts_repo.count_undelivered(conn)
    assert remaining == 2  # both still queued (delivered_utc still NULL)
