"""Alerts (Phase 6a).

Three evaluators + one delivery worker + one filesystem-less notification
sink (a Discord webhook). Everything routes through the same
`(rule_id, dedupe_key)` UNIQUE at the store layer so a rule that keeps
matching only produces one row per underlying event.

Design notes
------------

* **Price moves** are keyed on the *snapshot id* — a fresh snapshot with
  |change_pct| >= threshold fires exactly once, no matter how many times
  the evaluator runs before the next snapshot lands.
* **New filings** are keyed on `filings.id`. A rule that watches a ticker
  fires on every new-to-us filing that matches; a rule with
  `doc_types` narrows the match.
* **News** is keyed on `news_items.id`. Un-tagged rows (relevance IS NULL)
  are ignored — the min-relevance gate has nothing to compare against yet.
* **Delivery** is idempotent by design: `delivered_utc IS NULL` is the
  queue, and the worker only marks rows delivered after a 2xx from the
  webhook. A webhook outage does not lose events.
* **No Discord? Still safe.** With `DISCORD_WEBHOOK_URL` unset, delivery
  no-ops (marks events `skipped`) so a fresh install doesn't accumulate
  a growing queue.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from brvm.clock import utc_iso
from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.models import AlertEvent, AlertRule
from brvm.store import alerts as alerts_repo

log = get(__name__)


@dataclass
class EvalCounts:
    price_move_fired: int = 0
    new_filing_fired: int = 0
    news_fired: int = 0
    total_deduped: int = 0
    rules_considered: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "price_move_fired": self.price_move_fired,
            "new_filing_fired": self.new_filing_fired,
            "news_fired": self.news_fired,
            "total_deduped": self.total_deduped,
            "rules_considered": self.rules_considered,
        }


@dataclass
class DeliveryCounts:
    considered: int = 0
    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    reason: str = ""

    def as_dict(self) -> dict[str, str | int]:
        d: dict[str, str | int] = {
            "considered": self.considered,
            "delivered": self.delivered,
            "failed": self.failed,
            "skipped": self.skipped,
        }
        if self.reason:
            d["reason"] = self.reason
        return d


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------


def _db_path() -> Path:
    return Path(settings.db_path)


def _rules_by_kind(
    rules: list[AlertRule], kind: str
) -> list[AlertRule]:
    return [r for r in rules if r.kind == kind and r.enabled]


def _price_move_dedupe(rule_id: int, ticker: str, captured_utc: str) -> str:
    # `quote_snapshots` has no synthetic id — its PK is (ticker,
    # captured_utc, source). Ticker + captured_utc is enough because the
    # scheduler only ingests one source per snapshot cycle.
    return f"snap:{ticker}:{captured_utc}"


def _new_filing_dedupe(rule_id: int, filing_id: int) -> str:
    return f"filing:{filing_id}"


def _news_dedupe(rule_id: int, news_id: int) -> str:
    return f"news:{news_id}"


def _price_move_body(row: sqlite3.Row) -> tuple[str, str, dict[str, object]]:
    ticker = row["ticker"]
    pct = row["change_pct"]
    last = row["last"]
    direction = "up" if (pct or 0) >= 0 else "down"
    subject = f"[{ticker}] {direction} {pct:+.2f}% at {last:,.0f} XOF"
    body = (
        f"{ticker} moved {pct:+.2f}% (last {last:,.0f} XOF, "
        f"vol {row['volume'] or 0:,}). Snapshot at {row['captured_utc']}."
    )
    payload: dict[str, object] = {
        "ticker": ticker,
        "change_pct": pct,
        "last": last,
        "volume": row["volume"],
        "turnover": row["turnover"],
        "captured_utc": row["captured_utc"],
    }
    return subject, body, payload


def evaluate_price_moves(
    conn: sqlite3.Connection, rules: list[AlertRule]
) -> tuple[int, int]:
    """One eval per (rule, latest snapshot). Rule with `ticker=None`
    scans every snapshot; a rule with a ticker only scans that one row.
    Returns (fired, deduped)."""
    price_rules = _rules_by_kind(rules, "price_move")
    if not price_rules:
        return 0, 0

    snapshots = {
        row["ticker"]: row for row in _latest_snapshots(conn)
    }
    fired = 0
    deduped = 0
    for rule in price_rules:
        if rule.threshold_pct is None:
            log.warning("price_move rule %s missing threshold_pct — skipping", rule.id)
            continue
        thr = abs(rule.threshold_pct)
        candidates = (
            [snapshots[rule.ticker]] if rule.ticker and rule.ticker in snapshots
            else list(snapshots.values()) if not rule.ticker
            else []
        )
        for snap in candidates:
            pct = snap["change_pct"]
            if pct is None or abs(pct) < thr:
                continue
            subject, body, payload = _price_move_body(snap)
            new_id = alerts_repo.record_event(
                conn,
                rule_id=rule.id or 0,
                kind="price_move",
                ticker=snap["ticker"],
                subject=subject,
                body=body,
                payload=payload,
                dedupe_key=_price_move_dedupe(
                    rule.id or 0, snap["ticker"], snap["captured_utc"]
                ),
            )
            if new_id is not None:
                fired += 1
            else:
                deduped += 1
    return fired, deduped


def _latest_snapshots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Newest snapshot per ticker with all columns the alert body needs
    (including the row id, which is the price_move dedupe anchor)."""
    return list(
        conn.execute(
            """
            WITH latest AS (
                SELECT ticker, MAX(captured_utc) AS captured_utc
                FROM quote_snapshots
                GROUP BY ticker
            )
            SELECT qs.*
            FROM quote_snapshots qs
            JOIN latest l USING (ticker, captured_utc)
            """
        ).fetchall()
    )


def _rule_watches(rule: AlertRule, ticker: str | None) -> bool:
    """A rule with `ticker=None` matches any ticker; otherwise exact match."""
    if rule.ticker is None:
        return True
    return rule.ticker == ticker


def _doc_types_set(rule: AlertRule) -> set[str] | None:
    if not rule.doc_types:
        return None
    return {t.strip() for t in rule.doc_types.split(",") if t.strip()}


def _new_filing_body(row: sqlite3.Row) -> tuple[str, str, dict[str, object]]:
    ticker = row["ticker"]
    subject = f"[{ticker}] new filing: {row['doc_type']} · {row['period_label'] or ''}".rstrip(" ·")
    body = (
        f"{ticker} · {row['doc_type']} · "
        f"{row['period_label'] or '—'}\n"
        f"Published: {row['published_date'] or 'unknown'}\n"
        f"Source: {row['source_url']}"
    )
    payload = {
        "ticker": ticker,
        "doc_type": row["doc_type"],
        "period_year": row["period_year"],
        "period_kind": row["period_kind"],
        "source_url": row["source_url"],
    }
    return subject, body, payload


def evaluate_new_filings(
    conn: sqlite3.Connection, rules: list[AlertRule], *, since_utc: str | None = None
) -> tuple[int, int]:
    """Fire on every filing whose fetched_utc >= since_utc. Passing None
    scans the whole table — dedupe by filing.id makes that safe on
    startup; every subsequent pass only sees new rows because the last
    ones already have a matching event row."""
    filing_rules = _rules_by_kind(rules, "new_filing")
    if not filing_rules:
        return 0, 0

    if since_utc:
        rows = conn.execute(
            "SELECT * FROM filings WHERE fetched_utc >= ? ORDER BY id",
            (since_utc,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM filings ORDER BY id").fetchall()

    fired = 0
    deduped = 0
    for row in rows:
        for rule in filing_rules:
            if not _rule_watches(rule, row["ticker"]):
                continue
            doc_types = _doc_types_set(rule)
            if doc_types and row["doc_type"] not in doc_types:
                continue
            subject, body, payload = _new_filing_body(row)
            new_id = alerts_repo.record_event(
                conn,
                rule_id=rule.id or 0,
                kind="new_filing",
                ticker=row["ticker"],
                subject=subject,
                body=body,
                payload=payload,
                dedupe_key=_new_filing_dedupe(rule.id or 0, row["id"]),
            )
            if new_id is not None:
                fired += 1
            else:
                deduped += 1
    return fired, deduped


def _news_body(row: sqlite3.Row) -> tuple[str, str, dict[str, object]]:
    tickers_llm = row["tickers_llm"] or ""
    subject = f"[news · {row['category_llm'] or 'other'}] {row['title']}"
    body = (
        f"{row['title']}\n"
        f"Relevance: {row['relevance']} · Category: {row['category_llm'] or 'other'}\n"
        f"Tickers: {tickers_llm or row['ticker_hint'] or '—'}\n"
        f"{row['summary_en'] or row['chapeau'] or ''}\n"
        f"Source: {row['url']}"
    ).strip()
    payload = {
        "news_id": row["id"],
        "relevance": row["relevance"],
        "category": row["category_llm"],
        "tickers_llm": tickers_llm,
        "ticker_hint": row["ticker_hint"],
        "url": row["url"],
    }
    return subject, body, payload


def _news_matches_ticker(row: sqlite3.Row, ticker: str | None) -> bool:
    if ticker is None:
        return True
    if row["ticker_hint"] == ticker:
        return True
    csv = row["tickers_llm"] or ""
    if not csv:
        return False
    return ticker in {t.strip() for t in csv.split(",")}


def evaluate_news(
    conn: sqlite3.Connection, rules: list[AlertRule]
) -> tuple[int, int]:
    """Only tagged rows (relevance IS NOT NULL) can match — a rule that
    reads min_relevance has nothing to compare against on an untagged row.
    The store-side UNIQUE(rule_id, dedupe_key) keeps the pass idempotent."""
    news_rules = _rules_by_kind(rules, "news")
    if not news_rules:
        return 0, 0

    rows = conn.execute(
        """
        SELECT id, source, kind, url, title, chapeau, issuer_name,
               ticker_hint, tickers_llm, relevance, category_llm,
               summary_fr, summary_en, published_at, fetched_utc
        FROM news_items
        WHERE relevance IS NOT NULL
        ORDER BY id
        """
    ).fetchall()

    fired = 0
    deduped = 0
    for row in rows:
        for rule in news_rules:
            floor = rule.min_relevance if rule.min_relevance is not None else 0
            if (row["relevance"] or 0) < floor:
                continue
            # A row can carry multiple tickers via `tickers_llm`. For a
            # rule watching a specific ticker, require the ticker to
            # appear either as ticker_hint or in the LLM CSV.
            if not _news_matches_ticker(row, rule.ticker):
                continue
            subject, body, payload = _news_body(row)
            # Attribute the event to the rule's ticker if set, else to
            # the row's primary ticker attribution.
            attributed = rule.ticker or row["ticker_hint"]
            if not attributed and row["tickers_llm"]:
                attributed = row["tickers_llm"].split(",", 1)[0].strip() or None
            new_id = alerts_repo.record_event(
                conn,
                rule_id=rule.id or 0,
                kind="news",
                ticker=attributed,
                subject=subject,
                body=body,
                payload=payload,
                dedupe_key=_news_dedupe(rule.id or 0, row["id"]),
            )
            if new_id is not None:
                fired += 1
            else:
                deduped += 1
    return fired, deduped


def evaluate_all() -> EvalCounts:
    """One pass over every enabled rule. Safe to call on an empty DB."""
    counts = EvalCounts()
    with connect(_db_path()) as conn:
        rules = alerts_repo.list_rules(conn, enabled_only=True)
        counts.rules_considered = len(rules)
        if not rules:
            log.info("alerts eval: no enabled rules")
            return counts
        pm_fired, pm_dupe = evaluate_price_moves(conn, rules)
        nf_fired, nf_dupe = evaluate_new_filings(conn, rules)
        n_fired, n_dupe = evaluate_news(conn, rules)
        counts.price_move_fired = pm_fired
        counts.new_filing_fired = nf_fired
        counts.news_fired = n_fired
        counts.total_deduped = pm_dupe + nf_dupe + n_dupe
    log.info("alerts eval: %s", counts.as_dict())
    return counts


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _format_discord(event: AlertEvent) -> dict[str, object]:
    """Discord webhook accepts `content` (plain text) or `embeds`.
    Terminal aesthetic stays simpler with plain content — the subject
    goes bold via markdown, then a codeblock-friendly body."""
    return {
        "content": f"**{event.subject}**\n{event.body}",
        "username": "brvm-terminal",
    }


@dataclass
class _DiscordSender:
    webhook_url: str
    client: httpx.Client | None = None
    _owned: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.Client(timeout=settings.http_timeout_s)
            self._owned = True

    def close(self) -> None:
        if self._owned and self.client is not None:
            self.client.close()

    def send(self, event: AlertEvent) -> tuple[bool, str]:
        assert self.client is not None
        try:
            resp = self.client.post(self.webhook_url, json=_format_discord(event))
            resp.raise_for_status()
        except httpx.HTTPError as e:
            return False, f"http_error: {e}"
        return True, "ok"


def deliver_pending(
    *,
    sender: _DiscordSender | None = None,
    limit: int | None = None,
) -> DeliveryCounts:
    """Drain the un-delivered queue via Discord. Callers can pass a fake
    sender to unit-test the flow; production wires `sender=None` and
    reads the webhook URL from settings."""
    counts = DeliveryCounts()
    batch = limit or settings.alerts_delivery_batch
    with connect(_db_path()) as conn:
        events = alerts_repo.list_undelivered(conn, limit=batch)
        counts.considered = len(events)
        if not events:
            return counts

        # No webhook configured → mark events skipped so we don't keep
        # scanning the same queue forever. Manual delivery would clear
        # them later.
        if sender is None and not settings.has_discord:
            counts.skipped = len(events)
            counts.reason = "no_webhook"
            alerts_repo.mark_delivered(
                conn, [e.id or 0 for e in events], status="skipped"
            )
            log.warning("alerts deliver: no DISCORD_WEBHOOK_URL — %d events skipped",
                        len(events))
            return counts

        owns_sender = sender is None
        if sender is None:
            sender = _DiscordSender(webhook_url=settings.discord_webhook_url)

        try:
            delivered_ids: list[int] = []
            failed_ids: list[int] = []
            for event in events:
                ok, note = sender.send(event)
                if ok:
                    delivered_ids.append(event.id or 0)
                else:
                    failed_ids.append(event.id or 0)
                    log.warning("alerts deliver: event %s failed: %s",
                                event.id, note)
                    # Stop on the first failure — a 5xx/timeout usually
                    # means the webhook is down; better to retry the
                    # whole batch next pass than spam failed rows.
                    break

            if delivered_ids:
                alerts_repo.mark_delivered(conn, delivered_ids, status="ok")
                counts.delivered = len(delivered_ids)
            if failed_ids:
                # Leave delivered_utc NULL so the next pass re-tries the
                # same rows; only stamp status for diagnostics.
                conn.execute(
                    "UPDATE alert_events SET delivery_status = 'failed' "
                    f"WHERE id IN ({','.join('?' * len(failed_ids))})",
                    failed_ids,
                )
                conn.commit()
                counts.failed = len(failed_ids)
                counts.reason = "http_error"
        finally:
            if owns_sender:
                sender.close()

    log.info("alerts deliver: %s", counts.as_dict())
    return counts


# ---------------------------------------------------------------------------
# Read helpers for the UI
# ---------------------------------------------------------------------------


def list_rules(*, enabled_only: bool = False) -> list[AlertRule]:
    with connect(_db_path()) as conn:
        return alerts_repo.list_rules(conn, enabled_only=enabled_only)


def list_recent_events(*, limit: int = 25) -> list[AlertEvent]:
    with connect(_db_path()) as conn:
        return alerts_repo.list_recent(conn, limit=limit)


def create_rule(rule: AlertRule) -> int:
    with connect(_db_path()) as conn:
        return alerts_repo.create_rule(conn, rule)


def set_enabled(rule_id: int, enabled: bool) -> int:
    with connect(_db_path()) as conn:
        return alerts_repo.set_enabled(conn, rule_id, enabled)


def delete_rule(rule_id: int) -> int:
    with connect(_db_path()) as conn:
        return alerts_repo.delete_rule(conn, rule_id)


__all__ = [
    "DeliveryCounts",
    "EvalCounts",
    "create_rule",
    "delete_rule",
    "deliver_pending",
    "evaluate_all",
    "evaluate_new_filings",
    "evaluate_news",
    "evaluate_price_moves",
    "list_recent_events",
    "list_rules",
    "set_enabled",
    "utc_iso",  # re-exported so tests can freeze it if needed
]
