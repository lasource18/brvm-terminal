"""Tests for services/tagging.py: the incremental Haiku tagging worker."""

from __future__ import annotations

import importlib
from pathlib import Path

from brvm.db import connect
from brvm.models import NewsItem, Security
from brvm.sources._dedupe import news_hash
from brvm.store import news as news_repo
from brvm.store import securities as sec_repo
from brvm.store import spend as spend_repo

from ._fake_anthropic import FakeAnthropic, echoing_client, json_reply, reply, tag_for
from .conftest import apply_migrations


def _mk(title: str, **extra) -> NewsItem:
    url = f"https://www.sikafinance.com/marches/{title[:12].replace(' ', '_')}"
    return NewsItem(
        source="sikafinance",
        kind=extra.pop("kind", "news"),
        url=url,
        url_hash=news_hash(url, title),
        title=title,
        published_at="2026-08-20T09:00:00Z",
        **extra,
    )


def _setup(monkeypatch, tmp_path: Path, n_items: int = 5):
    """Fresh DB + seeded securities + `n_items` untagged news rows."""
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))

    import brvm.config as cfg

    importlib.reload(cfg)
    import brvm.services.llm as llm_mod
    import brvm.services.tagging as svc

    importlib.reload(llm_mod)
    importlib.reload(svc)

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(
            conn,
            [
                Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
                Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
                Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
            ],
        )
        news_repo.upsert_news_items(conn, [_mk(f"Article numéro {i}") for i in range(n_items)])
    return db_path, svc


def test_tags_every_pending_item_and_records_spend(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_items=5)

    counts = svc.tag_pending(batch_size=2, client=echoing_client())

    assert counts["pending_before"] == 5
    assert counts["batches"] == 3  # 2 + 2 + 1
    assert counts["tagged"] == 5
    assert counts["unanswered"] == 0
    assert counts["pending_after"] == 0
    assert counts["spend_micros_after"] == 3 * 3000

    with connect(db_path) as conn:
        rows = news_repo.list_news(conn, limit=10)
        assert all(r["tagged_utc"] for r in rows)
        assert all(r["category_llm"] == "earnings" for r in rows)
        assert all(r["tickers_llm"] == "SNTS" for r in rows)
        assert all(r["summary_fr"] and r["summary_en"] for r in rows)
        # The CSV write is what makes the per-ticker news query work.
        assert len(news_repo.list_news(conn, ticker="SNTS")) == 5
        assert spend_repo.get_day(conn)["calls"] == 3


def test_second_pass_is_a_no_op(monkeypatch, tmp_path):
    _db, svc = _setup(monkeypatch, tmp_path, n_items=3)
    svc.tag_pending(batch_size=8, client=echoing_client())

    # A client with no scripted replies would raise if it were called.
    counts = svc.tag_pending(batch_size=8, client=FakeAnthropic([]))
    assert counts["pending_before"] == 0
    assert counts["batches"] == 0
    assert counts["tagged"] == 0


def test_items_the_model_skips_are_still_stamped(monkeypatch, tmp_path):
    """Otherwise an item the model keeps ignoring is billed on every pass."""
    db_path, svc = _setup(monkeypatch, tmp_path, n_items=3)
    with connect(db_path) as conn:
        ids = [r["id"] for r in news_repo.list_untagged(conn)]

    client = FakeAnthropic([json_reply([tag_for(ids[0])])])
    counts = svc.tag_pending(batch_size=8, client=client)

    assert counts["tagged"] == 1
    assert counts["unanswered"] == 2
    assert counts["pending_after"] == 0
    with connect(db_path) as conn:
        skipped = conn.execute("SELECT * FROM news_items WHERE id = ?", (ids[1],)).fetchone()
        assert skipped["tagged_utc"] is not None
        assert skipped["category_llm"] is None


def test_daily_cap_stops_the_pass(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_items=6)
    with connect(db_path) as conn:
        # Burn the whole $1 before we start.
        spend_repo.add_usage(conn, input_tokens=0, output_tokens=0, usd_micros=1_000_000)

    counts = svc.tag_pending(batch_size=2, client=FakeAnthropic([]))

    assert counts["tagged"] == 0
    assert counts["skipped_budget"] == 6
    assert counts["pending_after"] == 6


def test_cap_stops_mid_pass_once_crossed(monkeypatch, tmp_path):
    db_path, svc = _setup(monkeypatch, tmp_path, n_items=6)
    with connect(db_path) as conn:
        spend_repo.add_usage(conn, input_tokens=0, output_tokens=0, usd_micros=999_000)

    # First batch fits in the last $0.001; it then pushes spend over the cap.
    counts = svc.tag_pending(batch_size=2, client=echoing_client())

    assert counts["tagged"] == 2
    assert counts["skipped_budget"] == 4
    assert counts["pending_after"] == 4


def test_failed_batch_leaves_items_untagged_and_still_bills(monkeypatch, tmp_path):
    _db, svc = _setup(monkeypatch, tmp_path, n_items=2)
    client = FakeAnthropic([reply("garbage"), reply("more garbage")])

    counts = svc.tag_pending(batch_size=2, client=client)

    assert counts["failed_batches"] == 1
    assert counts["tagged"] == 0
    assert counts["pending_after"] == 2  # retried on the next pass
    assert counts["spend_micros_after"] == 6000  # both attempts were billed


def test_pass_aborts_after_consecutive_failures(monkeypatch, tmp_path):
    _db, svc = _setup(monkeypatch, tmp_path, n_items=10)
    # Every call raises a transport error -> 3 failed batches then abort.
    client = FakeAnthropic([RuntimeError("boom")] * 20)

    counts = svc.tag_pending(batch_size=1, client=client)

    assert counts["failed_batches"] == 3
    assert client.call_count == 3  # not 10


def test_no_api_key_is_a_warning_not_a_crash(monkeypatch, tmp_path):
    _db, svc = _setup(monkeypatch, tmp_path, n_items=3)
    monkeypatch.setattr(svc.settings, "anthropic_api_key", "")

    counts = svc.tag_pending(batch_size=2)

    assert counts["llm_disabled"] == 1
    assert counts["tagged"] == 0
    assert counts["pending_after"] == 3


def test_dry_run_spends_nothing(monkeypatch, tmp_path):
    _db, svc = _setup(monkeypatch, tmp_path, n_items=5)

    counts = svc.tag_pending(batch_size=2, dry_run=True, client=FakeAnthropic([]))

    assert counts["dry_run"] == 1
    assert counts["batches"] == 3
    assert counts["tagged"] == 0
    assert counts["spend_micros_after"] == 0
    assert counts["pending_after"] == 5


def test_limit_caps_items_processed(monkeypatch, tmp_path):
    _db, svc = _setup(monkeypatch, tmp_path, n_items=6)

    counts = svc.tag_pending(limit=2, batch_size=8, client=echoing_client())

    assert counts["tagged"] == 2
    assert counts["pending_after"] == 4


def test_universe_sent_to_the_model_comes_from_the_db(monkeypatch, tmp_path):
    _db, svc = _setup(monkeypatch, tmp_path, n_items=1)
    client = echoing_client()
    svc.tag_pending(batch_size=8, client=client)

    system = client.calls[0]["system"][0]["text"]
    assert "SNTS\tSONATEL" in system
    assert "BRVMC\tBRVM COMPOSITE" in system
