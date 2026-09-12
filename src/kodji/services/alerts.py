"""Alerts (Phase 6a).

Three evaluators + one delivery worker that fans each event out to the
owning account's members — Web Push to every device they enabled, email
for members with none (PR-AA). Everything routes through the same
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
  queue, and the worker only marks a row delivered after a push service
  or the mailer accepted at least one send. An outage does not lose
  events.
* **No channel? Still safe.** With neither VAPID keys nor email
  configured, delivery marks events `skipped` so a fresh install doesn't
  accumulate a growing queue.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Protocol

from kodji.clock import ABIDJAN, utc_iso
from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.models import AlertEvent, AlertRule, PushSubscription
from kodji.services import webpush
from kodji.services.mailer import EmailMessage, Mailer, get_mailer
from kodji.store import accounts as accounts_repo
from kodji.store import alerts as alerts_repo
from kodji.store import push as push_repo

log = get(__name__)


class PushSenderLike(Protocol):
    """What delivery needs from a push sender — `WebPushSender` in
    production, a scripted stub in tests."""

    def send(self, sub: PushSubscription, payload: dict[str, object]) -> webpush.PushResult: ...

    def close(self) -> None: ...


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
    delivered: int = 0          # events with at least one successful send
    failed: int = 0
    skipped: int = 0
    pushed: int = 0             # individual push sends that landed
    emailed: int = 0            # individual emails that went out
    reason: str = ""

    def as_dict(self) -> dict[str, str | int]:
        d: dict[str, str | int] = {
            "considered": self.considered,
            "delivered": self.delivered,
            "failed": self.failed,
            "skipped": self.skipped,
            "pushed": self.pushed,
            "emailed": self.emailed,
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


def _price_move_session_bucket(captured_utc: str) -> str:
    """Trading session this snapshot belongs to (YYYY-MM-DD in Abidjan).
    Weekend captures collapse to the preceding Friday so re-scrapes of
    Friday's close over a full weekend dedupe onto one bucket instead of
    firing on every hourly cycle."""
    dt = datetime.fromisoformat(captured_utc.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(ABIDJAN).date()
    if local.weekday() >= 5:  # Sat/Sun → walk back to Fri
        local -= timedelta(days=local.weekday() - 4)
    return local.isoformat()


def _price_move_dedupe(rule_id: int, ticker: str, captured_utc: str) -> str:
    # Dedupe by (ticker, trading session) — one alert per rule per ticker
    # per session, no matter how many snapshots the scheduler captures.
    # The 0009_alerts.sql schema comment promised this bucket; the raw
    # `captured_utc` alone let a Friday close re-fire ~40 times over the
    # weekend since every scrape stamps a new timestamp.
    return f"snap:{ticker}:{_price_move_session_bucket(captured_utc)}"


def _new_filing_dedupe(rule_id: int, filing_id: int) -> str:
    return f"filing:{filing_id}"


def _news_dedupe(rule_id: int, news_id: int) -> str:
    return f"news:{news_id}"


def _fmt_price(v: float | None) -> str:
    return f"{v:,.0f} XOF" if v is not None else "—"


def _price_move_body(row: sqlite3.Row) -> tuple[str, str, dict[str, object]]:
    ticker = row["ticker"]
    pct = row["change_pct"]
    last = row["last"]
    direction = "up" if (pct or 0) >= 0 else "down"
    subject = f"[{ticker}] {direction} {pct:+.2f}% at {_fmt_price(last)}"
    body = (
        f"{ticker} moved {pct:+.2f}% (last {_fmt_price(last)}, "
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
            # F-16: only rows ingested after the rule was created are
            # eligible. Without this, adding a wildcard filing rule
            # would replay years of historical filings into the
            # delivery queue at 10 events per 5 min (~50 min of spam).
            if (
                rule.created_utc is not None
                and row["fetched_utc"] is not None
                and row["fetched_utc"] < rule.created_utc
            ):
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
            # F-16: only news ingested after the rule was created are
            # eligible. Prevents a fresh news rule from re-firing on
            # the tagged historical corpus.
            if (
                rule.created_utc is not None
                and row["fetched_utc"] is not None
                and row["fetched_utc"] < rule.created_utc
            ):
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
        # Cross-account by design: the scheduler evaluates every
        # customer's rules in one pass. Named distinctly from the
        # account-scoped `list_rules` so this stays the only such read.
        rules = alerts_repo.list_all_enabled_rules(conn)
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
# Delivery (PR-AA: Web Push to every member device, email otherwise)
# ---------------------------------------------------------------------------
#
# An event belongs to a rule, a rule to an account, an account to its
# members, and each member to zero or more devices that enabled
# notifications. One queued row therefore fans out to N sends:
#
#   * a member with devices on file gets a push on each of them;
#   * a member with none gets an email, when email is configured;
#   * an account with no members reachable either way is `skipped`.
#
# The event row keeps one status. It is `ok` as soon as one send lands —
# a phone that was off gets nothing, but the person was reached — and
# stays queued only when every attempt failed transiently (push service
# or mailer down), in which case the pass stops so a down provider is not
# hammered with the rest of the batch. All-permanent failures leave the
# queue as `permanent_failure`, exactly as the Discord path did.
#
# Discord is not a user channel any more; the job watchdog still posts
# there (services/watchdog).


def _link_path(event: AlertEvent) -> str:
    """Where a reader should land: the security if the event names one,
    the alerts queue otherwise. Same target on both channels."""
    return f"/s/{event.ticker}" if event.ticker else "/alerts"


def _push_payload(event: AlertEvent) -> dict[str, object]:
    """What the service worker shows. Bodies are clipped so a long filing
    title cannot push the record over the push service's 4 KB cap."""
    return {
        "title": event.subject[:120],
        "body": event.body[:400],
        "url": _link_path(event),
        "tag": f"kodji-alert-{event.id or event.dedupe_key}",
        "kind": event.kind,
    }


def _email_for(event: AlertEvent, to: str) -> EmailMessage:
    base = settings.public_base_url.rstrip("/")
    link = f"{base}{_link_path(event)}" if base else ""
    text = event.body + (f"\n\n{link}" if link else "")
    html = (
        f"<p><strong>{escape(event.subject)}</strong></p>"
        f"<p>{escape(event.body).replace(chr(10), '<br>')}</p>"
        + (f'<p><a href="{escape(link)}">{escape(link)}</a></p>' if link else "")
    )
    return EmailMessage(to=to, subject=f"[kodji] {event.subject}"[:200], text=text, html=html)


@dataclass
class _Attempt:
    ok: bool
    permanent: bool
    note: str


def _deliver_one(
    conn: sqlite3.Connection,
    event: AlertEvent,
    *,
    sender: PushSenderLike | None,
    mailer: Mailer | None,
    counts: DeliveryCounts,
) -> list[_Attempt]:
    """Fan one event out. Returns every attempt; the caller decides the
    row's status from the set."""
    account_id = alerts_repo.account_for_rule(conn, event.rule_id)
    if account_id is None:
        return []
    subs_by_user: dict[int, list[PushSubscription]] = {}
    if sender is not None:
        for sub in push_repo.list_for_account(conn, account_id):
            subs_by_user.setdefault(sub.user_id, []).append(sub)

    attempts: list[_Attempt] = []
    for member in accounts_repo.members(conn, account_id):
        user_id, email = int(member["user_id"]), str(member["email"])
        devices = subs_by_user.get(user_id, [])
        if devices and sender is not None:
            for sub in devices:
                r = sender.send(sub, _push_payload(event))
                attempts.append(_Attempt(r.ok, r.permanent, r.note))
                if r.ok:
                    counts.pushed += 1
                    push_repo.mark_result(conn, sub.id or 0, ok=True, commit=False)
                elif r.gone:
                    # 404/410: the browser revoked or rotated it. Keeping
                    # the row would fail every pass forever.
                    push_repo.delete_by_id(conn, sub.id or 0, commit=False)
                    log.info("alerts deliver: dropped dead push subscription %s (%s)", sub.id, r.note)
                else:
                    push_repo.mark_result(conn, sub.id or 0, ok=False, note=r.note, commit=False)
                    log.warning("alerts deliver: push to subscription %s failed: %s", sub.id, r.note)
            continue
        if mailer is not None:
            r = mailer.send(_email_for(event, email))
            attempts.append(_Attempt(r.ok, r.permanent, r.note))
            if r.ok:
                counts.emailed += 1
            else:
                log.warning("alerts deliver: email for event %s failed: %s", event.id, r.note)
    conn.commit()
    return attempts


def deliver_pending(
    *,
    sender: PushSenderLike | None = None,
    mailer: Mailer | None = None,
    limit: int | None = None,
) -> DeliveryCounts:
    """Drain the un-delivered queue.

    Production passes nothing and builds the channels from settings:
    a `WebPushSender` when VAPID keys exist, the Resend mailer when
    email does. Tests inject fakes. With neither channel configured the
    batch is marked `skipped` so the queue does not grow forever.
    """
    counts = DeliveryCounts()
    batch = limit or settings.alerts_delivery_batch
    with connect(_db_path()) as conn:
        events = alerts_repo.list_undelivered(conn, limit=batch)
        counts.considered = len(events)
        if not events:
            return counts

        owns_sender = owns_mailer = False
        if sender is None and settings.has_push:
            sender = webpush.sender_from_settings()
            owns_sender = True
        if mailer is None and settings.has_email:
            mailer = get_mailer()
            owns_mailer = True
        if sender is None and mailer is None:
            counts.skipped = len(events)
            counts.reason = "no_channel"
            alerts_repo.mark_delivered(conn, [e.id or 0 for e in events], status="skipped")
            log.warning(
                "alerts deliver: no VAPID keys and no email sender — %d events skipped",
                len(events),
            )
            return counts

        try:
            delivered_ids: list[int] = []
            skipped_ids: list[int] = []
            permanent_failed_ids: list[int] = []
            transient_failed_ids: list[int] = []
            reasons: list[str] = []
            for event in events:
                attempts = _deliver_one(conn, event, sender=sender, mailer=mailer, counts=counts)
                if not attempts:
                    skipped_ids.append(event.id or 0)
                    continue
                if any(a.ok for a in attempts):
                    delivered_ids.append(event.id or 0)
                    continue
                reasons.extend(a.note for a in attempts)
                if all(a.permanent for a in attempts):
                    permanent_failed_ids.append(event.id or 0)
                    continue
                # Something is down. Leave the row queued and stop the
                # pass; the rest of the batch retries in five minutes.
                transient_failed_ids.append(event.id or 0)
                break

            if delivered_ids:
                alerts_repo.mark_delivered(conn, delivered_ids, status="ok")
                counts.delivered = len(delivered_ids)
            if skipped_ids:
                alerts_repo.mark_delivered(conn, skipped_ids, status="skipped")
                counts.skipped = len(skipped_ids)
                counts.reason = counts.reason or "no_recipients"
            if permanent_failed_ids:
                alerts_repo.mark_delivered(conn, permanent_failed_ids, status="permanent_failure")
                counts.failed += len(permanent_failed_ids)
            if transient_failed_ids:
                conn.execute(
                    "UPDATE alert_events SET delivery_status = 'failed' "
                    f"WHERE id IN ({','.join('?' * len(transient_failed_ids))})",
                    transient_failed_ids,
                )
                conn.commit()
                counts.failed += len(transient_failed_ids)
            if counts.failed:
                counts.reason = reasons[0] if reasons else "send_error"
        finally:
            if owns_sender and sender is not None:
                sender.close()
            if owns_mailer and mailer is not None:
                mailer.close()

    log.info("alerts deliver: %s", counts.as_dict())
    return counts


# ---------------------------------------------------------------------------
# Read helpers for the UI
# ---------------------------------------------------------------------------


def list_rules(account_id: int, *, enabled_only: bool = False) -> list[AlertRule]:
    with connect(_db_path()) as conn:
        return alerts_repo.list_rules(conn, account_id, enabled_only=enabled_only)


def list_recent_events(*, limit: int = 25) -> list[AlertEvent]:
    with connect(_db_path()) as conn:
        return alerts_repo.list_recent(conn, limit=limit)


def create_rule(account_id: int, rule: AlertRule) -> int:
    with connect(_db_path()) as conn:
        return alerts_repo.create_rule(conn, account_id, rule)


def set_enabled(account_id: int, rule_id: int, enabled: bool) -> int:
    with connect(_db_path()) as conn:
        return alerts_repo.set_enabled(conn, account_id, rule_id, enabled)


def delete_rule(account_id: int, rule_id: int) -> int:
    with connect(_db_path()) as conn:
        return alerts_repo.delete_rule(conn, account_id, rule_id)


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
