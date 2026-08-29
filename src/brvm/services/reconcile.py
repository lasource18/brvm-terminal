"""Cross-check `daily_bars.close` against authoritative sources.

Phase 8f adds `check_boc_close()` — fetches today's BOC PDF from
brvm.org, extracts the equity-row close prices, and reports any
tickers where the local `daily_bars.close` (populated by the
sikafinance-driven Phase-1 snapshot pipeline) doesn't match the
official bulletin. Small drift is expected (rounding on multi-column
extractions, session-timing gaps); the check reports the delta and
lets the caller decide whether to flag.

Kept intentionally read-only: this service does not write anything back
into `daily_bars`. A future job can wire the mismatches into an alert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.sources import brvm_org

log = get(__name__)


@dataclass(frozen=True)
class CloseDrift:
    """One row where BOC and daily_bars disagree on today's close."""

    ticker: str
    boc_close: float
    local_close: float | None
    delta_pct: float | None   # (local - boc) / boc * 100; None when local is missing


@dataclass(frozen=True)
class ReconcileReport:
    session_date: date | None
    boc_rows: int
    matched: int
    drift: list[CloseDrift]

    @property
    def has_drift(self) -> bool:
        return len(self.drift) > 0


def _db_path() -> Path:
    return Path(settings.db_path)


def _latest_session() -> date | None:
    with connect(_db_path()) as conn:
        row = conn.execute(
            "SELECT MAX(session_date) AS d FROM daily_bars"
        ).fetchone()
    if row is None or row["d"] is None:
        return None
    return date.fromisoformat(row["d"])


def _local_closes_for_session(session_date: date) -> dict[str, float]:
    with connect(_db_path()) as conn:
        rows = conn.execute(
            "SELECT ticker, close FROM daily_bars WHERE session_date = ?",
            (session_date.isoformat(),),
        ).fetchall()
    return {r["ticker"]: r["close"] for r in rows}


def check_boc_close(
    *,
    tolerance_pct: float = 0.01,
    session_date: date | None = None,
    pdf_bytes: bytes | None = None,
) -> ReconcileReport:
    """Fetch today's BOC PDF (or use the caller-supplied bytes) and
    report tickers whose local `daily_bars.close` disagrees with the
    bulletin by more than `tolerance_pct` percent.

    `session_date` scopes the local lookup; when None, the BOC's own
    filename-encoded session date is used — the audit's F-04 root cause
    was falling back to `MAX(daily_bars.session_date)`, which for
    equity tickers is typically the most recent *weekly* backfill,
    days apart from the BOC's date. Tickers on the BOC but missing
    locally are surfaced with `local_close=None` and `delta_pct=None`
    so a reader can see the gap without a division-by-zero.
    """
    if pdf_bytes is None:
        fetched = brvm_org.fetch_boc(lang="eng")
        if fetched is None:
            log.warning("BOC PDF unavailable; skipping reconciliation")
            return ReconcileReport(
                session_date=session_date, boc_rows=0, matched=0, drift=[],
            )
        pdf_bytes = fetched.pdf_bytes
        boc_session = fetched.session_date
    else:
        boc_session = None
    if not pdf_bytes:
        log.warning("BOC PDF unavailable; skipping reconciliation")
        return ReconcileReport(
            session_date=session_date, boc_rows=0, matched=0, drift=[],
        )
    session = session_date or boc_session or _latest_session()
    if session is None:
        log.warning("no local daily_bars; nothing to reconcile")
        return ReconcileReport(
            session_date=None, boc_rows=0, matched=0, drift=[],
        )
    boc_rows = brvm_org.parse_boc_rows(pdf_bytes)
    local = _local_closes_for_session(session)
    drift: list[CloseDrift] = []
    matched = 0
    for r in boc_rows:
        local_close = local.get(r.ticker)
        if local_close is None:
            drift.append(CloseDrift(
                ticker=r.ticker, boc_close=r.close,
                local_close=None, delta_pct=None,
            ))
            continue
        if r.close == 0:
            # BOC close of zero would divide by zero — surface as missing
            # rather than fabricate a percent.
            drift.append(CloseDrift(
                ticker=r.ticker, boc_close=r.close,
                local_close=local_close, delta_pct=None,
            ))
            continue
        delta = (local_close - r.close) / r.close * 100.0
        if abs(delta) > tolerance_pct:
            drift.append(CloseDrift(
                ticker=r.ticker, boc_close=r.close,
                local_close=local_close, delta_pct=delta,
            ))
        else:
            matched += 1
    return ReconcileReport(
        session_date=session,
        boc_rows=len(boc_rows),
        matched=matched,
        drift=drift,
    )
