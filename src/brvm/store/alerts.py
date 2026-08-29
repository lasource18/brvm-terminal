"""SQLite repository for alert_rules + alert_events.

The store owns two invariants the evaluators (services/alerts.py) depend
on:

1. `(rule_id, dedupe_key)` is UNIQUE, so re-evaluation on the same
   underlying event (a snapshot id, a filing id, a news id) is a no-op.
   `record_event` returns whether the row was actually inserted so the
   caller can count fresh fires without a follow-up query.
2. `delivered_utc IS NULL` is the delivery queue — the delivery worker
   updates that column and the row moves out of the partial index. No
   separate queue table needed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from brvm.clock import utc_iso
from brvm.models import AlertEvent, AlertRule

# --- Rules -----------------------------------------------------------------

def _row_to_rule(r: sqlite3.Row) -> AlertRule:
    return AlertRule(
        id=r["id"],
        kind=r["kind"],
        ticker=r["ticker"],
        threshold_pct=r["threshold_pct"],
        min_relevance=r["min_relevance"],
        doc_types=r["doc_types"],
        label=r["label"],
        enabled=bool(r["enabled"]),
        created_utc=r["created_utc"],
    )


def create_rule(conn: sqlite3.Connection, rule: AlertRule) -> int:
    cur = conn.execute(
        """
        INSERT INTO alert_rules
            (kind, ticker, threshold_pct, min_relevance, doc_types,
             label, enabled, created_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rule.kind,
            rule.ticker,
            rule.threshold_pct,
            rule.min_relevance,
            rule.doc_types,
            rule.label,
            1 if rule.enabled else 0,
            utc_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_rules(
    conn: sqlite3.Connection, *, enabled_only: bool = False
) -> list[AlertRule]:
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = conn.execute(
        f"SELECT * FROM alert_rules {where} ORDER BY id"
    ).fetchall()
    return [_row_to_rule(r) for r in rows]


def get_rule(conn: sqlite3.Connection, rule_id: int) -> AlertRule | None:
    r = conn.execute(
        "SELECT * FROM alert_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    return _row_to_rule(r) if r else None


def set_enabled(conn: sqlite3.Connection, rule_id: int, enabled: bool) -> int:
    cur = conn.execute(
        "UPDATE alert_rules SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, rule_id),
    )
    conn.commit()
    return cur.rowcount


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> int:
    # ON DELETE CASCADE clears events; keep the deletion here small.
    cur = conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
    conn.commit()
    return cur.rowcount


# --- Events ----------------------------------------------------------------


def _row_to_event(r: sqlite3.Row) -> AlertEvent:
    return AlertEvent(
        id=r["id"],
        rule_id=r["rule_id"],
        kind=r["kind"],
        ticker=r["ticker"],
        subject=r["subject"],
        body=r["body"],
        payload_json=r["payload_json"],
        dedupe_key=r["dedupe_key"],
        fired_utc=r["fired_utc"],
        delivered_utc=r["delivered_utc"],
        delivery_status=r["delivery_status"],
    )


def record_event(
    conn: sqlite3.Connection,
    *,
    rule_id: int,
    kind: str,
    ticker: str | None,
    subject: str,
    body: str,
    payload: dict[str, Any] | None,
    dedupe_key: str,
) -> int | None:
    """Insert a fresh event. Returns the new row id, or None if the
    `(rule_id, dedupe_key)` pair already exists (dedupe hit)."""
    cur = conn.execute(
        """
        INSERT INTO alert_events
            (rule_id, kind, ticker, subject, body, payload_json,
             dedupe_key, fired_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rule_id, dedupe_key) DO NOTHING
        """,
        (
            rule_id,
            kind,
            ticker,
            subject,
            body,
            json.dumps(payload, ensure_ascii=False) if payload else None,
            dedupe_key,
            utc_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid) if cur.rowcount else None


def list_undelivered(
    conn: sqlite3.Connection, *, limit: int = 10
) -> list[AlertEvent]:
    rows = conn.execute(
        """
        SELECT * FROM alert_events
        WHERE delivered_utc IS NULL
        ORDER BY fired_utc, id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def mark_delivered(
    conn: sqlite3.Connection,
    event_ids: Iterable[int],
    *,
    status: str = "ok",
    commit: bool = True,
) -> int:
    ids = list(event_ids)
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE alert_events SET delivered_utc = ?, delivery_status = ? "
        f"WHERE id IN ({placeholders})",
        (utc_iso(), status, *ids),
    )
    if commit:
        conn.commit()
    return cur.rowcount


def list_recent(
    conn: sqlite3.Connection, *, limit: int = 25
) -> list[AlertEvent]:
    rows = conn.execute(
        "SELECT * FROM alert_events ORDER BY fired_utc DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def count_undelivered(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM alert_events WHERE delivered_utc IS NULL"
    ).fetchone()[0]
