"""News tagging worker (Phase 3b).

Pulls `news_items` rows that have never been tagged (`tagged_utc IS NULL`),
sends them to Haiku in batches, and writes back `tickers_llm`,
`relevance`, `category_llm`, `summary_fr`, `summary_en`.

Two invariants keep this from becoming a money pit:

1. **Never re-process an article.** Every item handed to a successful call
   gets `tagged_utc` stamped, even when the model returned nothing usable
   for it. The partial index `ix_news_items_untagged` makes the "what's
   left" query cheap.
2. **Hard daily cap.** The budget is checked in `llm_spend` before every
   batch and the real cost of every call is recorded straight after, so a
   crash mid-pass can't lose spend. Once the day is over
   `settings.llm_daily_cap_cents` (default 100 = $1) the worker no-ops
   with a warning until UTC midnight.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from kodji.clock import utc_iso, utcnow
from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.services import llm
from kodji.store import news as news_repo
from kodji.store import spend as spend_repo

log = get(__name__)


def _load_universe(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    """Ticker table for the prompt. Equities + indices; bonds are excluded
    (they aren't what news items are ever *about*, and they'd bloat the
    cached prefix)."""
    return [
        (r["ticker"], r["name"], r["sector"])
        for r in conn.execute(
            "SELECT ticker, name, sector FROM securities "
            "WHERE kind IN ('equity', 'index') ORDER BY ticker"
        )
    ]


def _to_items(rows: list[sqlite3.Row]) -> list[llm.TagItem]:
    return [
        llm.TagItem(
            id=r["id"],
            title=r["title"],
            kind=r["kind"],
            source=r["source"],
            chapeau=r["chapeau"],
            issuer_name=r["issuer_name"],
            published_at=r["published_at"],
            ticker_hint=r["ticker_hint"],
        )
        for r in rows
    ]


def _chunks(items: list[llm.TagItem], size: int) -> list[list[llm.TagItem]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _record(conn: sqlite3.Connection, usage: llm.Usage, day: str) -> int:
    """Persist one call's usage; returns the day's new total in micros."""
    if usage.calls == 0:
        return spend_repo.spent_micros(conn, day)
    return spend_repo.add_usage(
        conn,
        calls=usage.calls,
        input_tokens=usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        usd_micros=usage.usd_micros,
        day=day,
    )


def tag_pending(
    *,
    limit: int | None = None,
    batch_size: int | None = None,
    dry_run: bool = False,
    client: object | None = None,
) -> dict[str, int]:
    """Tag every untagged news item (up to `limit`). Returns row counts.

    Degrades quietly rather than raising: no API key, an exhausted budget
    and a failing API all return counts with the reason flagged, so the
    scheduler job stays a no-op instead of a crash loop.
    """
    batch_size = batch_size or settings.llm_batch_size
    day = utcnow().date().isoformat()
    counts: dict[str, int] = {
        "pending_before": 0,
        "batches": 0,
        "tagged": 0,
        "unanswered": 0,
        "failed_batches": 0,
        "skipped_budget": 0,
        "pending_after": 0,
        "spend_micros_before": 0,
        "spend_micros_after": 0,
        "llm_disabled": 0,
        "dry_run": int(dry_run),
    }

    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        counts["pending_before"] = news_repo.count_untagged(conn)
        counts["spend_micros_before"] = spend_repo.spent_micros(conn, day)
        counts["spend_micros_after"] = counts["spend_micros_before"]

        rows = news_repo.list_untagged(conn, limit=limit or 100)
        if not rows:
            counts["pending_after"] = counts["pending_before"]
            log.info("news tagging: nothing to do")
            return counts

        universe = _load_universe(conn)
        batches = _chunks(_to_items(rows), batch_size)
        counts["batches"] = len(batches)

        if dry_run:
            counts["pending_after"] = counts["pending_before"]
            log.info(
                "news tagging dry-run: %d item(s) in %d batch(es), system prompt %d chars",
                len(rows),
                len(batches),
                len(llm.build_system_prompt(universe)),
            )
            return counts

        if client is None and not settings.has_llm:
            counts["llm_disabled"] = 1
            counts["pending_after"] = counts["pending_before"]
            log.warning(
                "news tagging skipped: ANTHROPIC_API_KEY not set (%d item(s) pending)",
                counts["pending_before"],
            )
            return counts

        consecutive_failures = 0
        for batch in batches:
            if spend_repo.remaining_micros(conn, settings.llm_daily_cap_cents, day) <= 0:
                counts["skipped_budget"] += len(batch)
                continue

            try:
                result = llm.tag_batch(batch, universe, client=client)
            except llm.LLMResponseError as e:
                counts["spend_micros_after"] = _record(conn, e.usage, day)
                counts["failed_batches"] += 1
                consecutive_failures += 1
                log.warning("tagging batch failed: %s", e)
            except llm.LLMUnavailable as e:
                counts["llm_disabled"] = 1
                log.warning("news tagging stopped: %s", e)
                break
            except Exception as e:  # unexpected error path (transport now surfaces as LLMResponseError)
                counts["failed_batches"] += 1
                consecutive_failures += 1
                log.warning("tagging batch errored: %s", e)
            else:
                consecutive_failures = 0
                counts["spend_micros_after"] = _record(conn, result.usage, day)
                tagged, unanswered = _apply(conn, batch, result.tags)
                counts["tagged"] += tagged
                counts["unanswered"] += unanswered

            if consecutive_failures >= settings.llm_max_consecutive_failures:
                log.error(
                    "news tagging aborted after %d consecutive failed batches",
                    consecutive_failures,
                )
                break

        counts["pending_after"] = news_repo.count_untagged(conn)

    log.info("news tagging: %s", counts)
    return counts


def _apply(
    conn: sqlite3.Connection, batch: list[llm.TagItem], tags: list[llm.NewsTag]
) -> tuple[int, int]:
    """Write a batch's tags. Items the model skipped are still stamped
    `tagged_utc` (with NULL tags) so they never cost us a second call.
    """
    by_id = {t.id: t for t in tags}
    now = utc_iso()
    tagged = 0
    unanswered = 0
    for item in batch:
        tag = by_id.get(item.id)
        if tag is None:
            news_repo.apply_tags(conn, item.id, tagged_utc=now, commit=False)
            unanswered += 1
            continue
        news_repo.apply_tags(
            conn,
            item.id,
            tickers=tag.tickers,
            relevance=tag.relevance,
            category=tag.category,
            summary_fr=tag.summary_fr or None,
            summary_en=tag.summary_en or None,
            tagged_utc=now,
            commit=False,
        )
        tagged += 1
    conn.commit()
    return tagged, unanswered
