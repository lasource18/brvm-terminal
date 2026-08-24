"""Fundamentals worker + read side (Phase 4b).

Ties the 4a filings corpus to the 4b extractor and persists the result.
Also carries the small read helpers the UI consumes.

Two invariants keep this from becoming a money pit:

1. **Never re-process a filing.** Every filing handed to a successful
   extraction call gets `filings.extracted_utc` stamped, even when the
   model returned nothing useful. A parse-failure still stamps; only a
   pre-flight refusal (budget exhausted, empty text, too big) leaves
   the row alone for a future pass.
2. **Hard daily cap.** Budget is checked in `filings_spend` before every
   call and the real cost is recorded straight after. Once the day
   crosses `settings.llm_extract_daily_cap_cents` the worker no-ops
   with a warning until UTC midnight.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from brvm.clock import utcnow
from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.services import extraction
from brvm.store import filings as filings_repo
from brvm.store import financials as financials_repo
from brvm.store import spend as spend_repo

log = get(__name__)


@dataclass
class ExtractCounts:
    scanned: int = 0
    pending_before: int = 0
    considered: int = 0
    extracted: int = 0
    empty_payloads: int = 0     # a call succeeded but the extract had nothing usable
    failed: int = 0
    skipped_budget: int = 0
    skipped_missing_file: int = 0
    llm_disabled: int = 0
    dry_run: int = 0
    pending_after: int = 0
    spend_micros_before: int = 0
    spend_micros_after: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "pending_before": self.pending_before,
            "considered": self.considered,
            "extracted": self.extracted,
            "empty_payloads": self.empty_payloads,
            "failed": self.failed,
            "skipped_budget": self.skipped_budget,
            "skipped_missing_file": self.skipped_missing_file,
            "llm_disabled": self.llm_disabled,
            "dry_run": self.dry_run,
            "pending_after": self.pending_after,
            "spend_micros_before": self.spend_micros_before,
            "spend_micros_after": self.spend_micros_after,
        }


def _record(conn: sqlite3.Connection, usage: extraction.Usage, day: str) -> int:
    if usage.calls == 0:
        return spend_repo.spent_micros(conn, day, table="filings_spend")
    return spend_repo.add_usage(
        conn,
        calls=usage.calls,
        input_tokens=usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        usd_micros=usage.usd_micros,
        day=day,
        table="filings_spend",
    )


def _persist(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    extract: extraction.FundamentalsExtract,
) -> bool:
    """Write one filing's extract to the fundamentals tables. Returns True
    if anything usable landed (i.e. we know the period + at least one
    non-null financial line OR at least one segment/owner row)."""
    period_year = extract.period_year or row["period_year"]
    period_kind = extract.period_kind or row["period_kind"] or "annual"
    if period_year is None:
        # Without a period we can't key the row. Stamp the filing so we
        # don't retry, but count it as an empty payload.
        return False

    has_numbers = any(
        v is not None
        for v in (
            extract.revenue,
            extract.operating_income,
            extract.net_income,
            extract.total_assets,
            extract.total_equity,
            extract.eps,
            extract.dividend_per_share,
        )
    )
    if not (has_numbers or extract.segments or extract.ownership):
        return False

    financials_repo.replace_period(
        conn,
        filing_id=row["id"],
        financials=financials_repo.FinancialsRow(
            ticker=row["ticker"],
            period_year=period_year,
            period_kind=period_kind,
            currency=extract.currency or "XOF",
            revenue=extract.revenue,
            operating_income=extract.operating_income,
            net_income=extract.net_income,
            total_assets=extract.total_assets,
            total_equity=extract.total_equity,
            eps=extract.eps,
            dividend_per_share=extract.dividend_per_share,
        ),
        segments=[
            financials_repo.SegmentRow(
                name=s.name,
                segment_kind=s.segment_kind,
                revenue=s.revenue,
                share_pct=s.share_pct,
            )
            for s in extract.segments
        ],
        ownership=[
            financials_repo.OwnershipRow(
                holder=o.holder,
                share_pct=o.share_pct,
                shares=o.shares,
            )
            for o in extract.ownership
        ],
    )
    return True


def extract_pending(
    *,
    limit: int | None = None,
    dry_run: bool = False,
    client: Any | None = None,
    project_root: Path | None = None,
) -> ExtractCounts:
    """Run one extraction pass over unprocessed annual filings.

    Degrades quietly rather than raising: no API key, an exhausted
    budget, or a failing API all return counts with the reason flagged,
    so the scheduler stays a no-op instead of a crash loop."""
    root = project_root or Path.cwd()
    day = utcnow().date().isoformat()
    counts = ExtractCounts(dry_run=int(dry_run))

    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        # Default 200/pass so a catch-up run after `just filings-ocr` doesn't
        # need to be invoked 30x in a row. The daily $2 cap
        # (`llm_extract_daily_cap_cents`) is the real gate on spend — this
        # limit just bounds how many filings we probe with pypdf per call,
        # which is essentially free.
        rows = filings_repo.list_needing_extraction(conn, limit=limit or 200)
        counts.pending_before = len(rows)
        counts.spend_micros_before = spend_repo.spent_micros(conn, day, table="filings_spend")
        counts.spend_micros_after = counts.spend_micros_before

        if not rows:
            counts.pending_after = 0
            log.info("fundamentals extraction: nothing to do")
            return counts

        cap_cents = settings.llm_extract_daily_cap_cents

        if not dry_run and client is None and not settings.has_llm:
            counts.llm_disabled = 1
            counts.pending_after = counts.pending_before
            log.warning(
                "fundamentals extraction skipped: ANTHROPIC_API_KEY not set (%d filings pending)",
                counts.pending_before,
            )
            return counts

        for row in rows:
            counts.considered += 1
            pdf_path = _resolve_path(root, row["file_path"])
            if not pdf_path.exists():
                counts.skipped_missing_file += 1
                log.warning("filing %s missing on disk: %s", row["id"], pdf_path)
                continue

            try:
                parsed = extraction.extract_pdf_text(pdf_path)
            except Exception as e:
                log.warning("pypdf failed on filing %s: %s", row["id"], e)
                filings_repo.mark_extracted(conn, row["id"])
                counts.failed += 1
                continue

            if parsed.is_scanned:
                counts.scanned += 1
                if not dry_run:
                    filings_repo.mark_extracted(conn, row["id"], is_scanned=True)
                continue

            estimate = extraction.preflight(parsed.text)
            remaining = spend_repo.remaining_micros(
                conn, cap_cents, day, table="filings_spend"
            )
            if estimate.est_usd_micros > remaining:
                counts.skipped_budget += 1
                continue

            if dry_run:
                counts.extracted += 1  # would-have-been
                continue

            try:
                result = extraction.extract_filing(
                    ticker=row["ticker"],
                    issuer_name=row["issuer_name"],
                    pdf_text=parsed.text,
                    period_year_hint=row["period_year"],
                    period_kind_hint=row["period_kind"],
                    client=client,
                )
            except extraction.LLMResponseError as e:
                counts.spend_micros_after = _record(conn, e.usage, day)
                counts.failed += 1
                filings_repo.mark_extracted(conn, row["id"])
                log.warning("extraction failed for filing %s: %s", row["id"], e)
                continue
            except extraction.LLMUnavailable as e:
                counts.llm_disabled = 1
                log.warning("fundamentals extraction stopped: %s", e)
                break
            except Exception as e:  # API/network — nothing billed, keep the row for retry
                counts.failed += 1
                log.warning("extraction errored for filing %s: %s", row["id"], e)
                continue

            counts.spend_micros_after = _record(conn, result.usage, day)
            landed = _persist(conn, row, result.extract)
            filings_repo.mark_extracted(conn, row["id"])
            if landed:
                counts.extracted += 1
            else:
                counts.empty_payloads += 1

        counts.pending_after = len(filings_repo.list_needing_extraction(conn, limit=10_000))

    log.info("fundamentals extraction: %s", counts.as_dict())
    return counts


def _resolve_path(root: Path, file_path: str) -> Path:
    p = Path(file_path)
    return p if p.is_absolute() else (root / p)


# --------------------------------------------------------------------------
# Read helpers used by the web tabs
# --------------------------------------------------------------------------


@dataclass
class FinancialsSeries:
    """One P&L / balance-sheet row per period, most recent on the left."""

    ticker: str
    currency: str = "XOF"
    periods: list[int] = field(default_factory=list)                     # descending years
    metrics: dict[str, list[float | None]] = field(default_factory=dict)  # metric -> per-period
    source_filings: dict[int, int] = field(default_factory=dict)         # year -> filing_id

    @property
    def has_data(self) -> bool:
        return bool(self.periods)


@dataclass
class InterimSnapshot:
    """Most-recent interim (H1/Q1/Q3) reading, rendered as a card above the
    annual table. Kept separate because interim rows are period-to-date and
    would mislead if mixed into the year-over-year table."""

    ticker: str
    period_year: int
    period_kind: str        # 'H1' | 'Q1' | 'Q3' | 'other'
    currency: str = "XOF"
    metrics: dict[str, float | None] = field(default_factory=dict)
    filing_id: int | None = None

    @property
    def has_data(self) -> bool:
        return any(v is not None for v in self.metrics.values())


_METRIC_KEYS: tuple[str, ...] = (
    "revenue",
    "operating_income",
    "net_income",
    "total_assets",
    "total_equity",
    "eps",
    "dividend_per_share",
)


def get_financials_series(ticker: str, *, limit: int = 6) -> FinancialsSeries:
    """5-year (default) annual series for the Financials tab."""
    with connect(settings.db_path) as conn:
        rows = financials_repo.list_financials(conn, ticker, limit=limit)
    series = FinancialsSeries(ticker=ticker)
    if not rows:
        return series
    series.currency = rows[0]["currency"] or "XOF"
    series.periods = [int(r["period_year"]) for r in rows]
    series.source_filings = {int(r["period_year"]): int(r["filing_id"]) for r in rows}
    for key in _METRIC_KEYS:
        series.metrics[key] = [r[key] for r in rows]
    return series


def get_latest_interim(ticker: str) -> InterimSnapshot | None:
    """Most recent H1/Q1/Q3 row for the Financials tab's interim card.

    Returns None when no interim data has been extracted or when the
    latest annual row already covers a newer period (mixing a stale H1
    with a fresh annual is more noise than signal)."""
    with connect(settings.db_path) as conn:
        row = conn.execute(
            """
            SELECT ticker, period_year, period_kind, currency, revenue,
                   operating_income, net_income, total_assets, total_equity,
                   eps, dividend_per_share, filing_id
            FROM financials
            WHERE ticker = ? AND period_kind IN ('H1', 'Q1', 'Q3', 'other')
            ORDER BY period_year DESC,
                     CASE period_kind
                        WHEN 'Q3' THEN 4
                        WHEN 'H1' THEN 3
                        WHEN 'Q1' THEN 2
                        ELSE 1 END DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if row is None:
            return None
        latest_annual = financials_repo.latest_period(conn, ticker)

    interim_year = int(row["period_year"])
    if latest_annual is not None and int(latest_annual["period_year"]) >= interim_year:
        return None

    return InterimSnapshot(
        ticker=ticker,
        period_year=interim_year,
        period_kind=row["period_kind"],
        currency=row["currency"] or "XOF",
        metrics={k: row[k] for k in _METRIC_KEYS},
        filing_id=int(row["filing_id"]),
    )


@dataclass
class SegmentsView:
    ticker: str
    period_year: int | None = None
    period_kind: str = "annual"
    currency: str = "XOF"
    business: list[dict[str, Any]] = field(default_factory=list)
    geo: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.business or self.geo)


def get_segments(ticker: str) -> SegmentsView:
    """Latest-period business + geographic split for the Segments tab."""
    with connect(settings.db_path) as conn:
        latest = financials_repo.latest_period(conn, ticker)
        if latest is None:
            return SegmentsView(ticker=ticker)
        rows = financials_repo.list_segments(conn, ticker, int(latest["period_year"]))

    view = SegmentsView(
        ticker=ticker,
        period_year=int(latest["period_year"]),
        period_kind=latest["period_kind"],
        currency=latest["currency"] or "XOF",
    )
    for r in rows:
        bucket = view.business if r["segment_kind"] == "business" else view.geo
        bucket.append(
            {
                "name": r["name"],
                "revenue": r["revenue"],
                "share_pct": r["share_pct"],
            }
        )
    return view


@dataclass
class OwnershipView:
    ticker: str
    period_year: int | None = None
    period_kind: str = "annual"
    holders: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.holders)


def reset_shadowed_extractions(*, dry_run: bool = False) -> dict[str, int]:
    """Recover ownership + segments that were wiped before PR #13's fix.

    When two filings for the same `(ticker, period_year, period_kind)`
    triple both got extracted, whichever landed second wiped the other's
    segment / ownership rows via the old `replace_period` blanket DELETE
    (fixed in PR #13). This helper finds the resulting shadowed periods
    and clears `extracted_utc` on the richer filing so the next
    `just fundamentals-extract` run re-processes it — now with the
    preserve-on-empty logic in place, which will re-populate ownership
    and segments without clobbering the P&L numbers.

    Rank order (richer → poorer):
      rapport_annuel > etats_financiers > rapport_activites > resultats
      > rse > assemblee > autre

    A period is "shadowed" when the currently-persisted
    `financials.filing_id` points at a filing whose doc_type ranks
    lower than at least one other filing for the same triple. Those
    "richer" filings get their `extracted_utc` cleared. Idempotent —
    a rerun after `just fundamentals-extract` will find nothing to do.
    """
    # Rank: lower value = richer. SQLite lets us map via CASE.
    _rank_case = """
        CASE doc_type
            WHEN 'rapport_annuel'    THEN 1
            WHEN 'etats_financiers'  THEN 2
            WHEN 'rapport_activites' THEN 3
            WHEN 'resultats'         THEN 4
            WHEN 'rse'               THEN 5
            WHEN 'assemblee'         THEN 6
            ELSE 7
        END
    """
    counts = {"periods_shadowed": 0, "filings_reset": 0, "dry_run": int(dry_run)}

    with connect(settings.db_path) as conn:
        # Find every filing whose rank beats (or ties, but with a
        # different id) the filing currently persisted in financials for
        # the same period. Those are the ones to re-process.
        rows = conn.execute(
            f"""
            SELECT candidate.id AS filing_id, candidate.ticker, candidate.doc_type,
                   candidate.period_year, candidate.period_kind,
                   persisted.doc_type AS persisted_doc_type
            FROM filings candidate
            JOIN financials f
              ON f.ticker      = candidate.ticker
             AND f.period_year = candidate.period_year
             AND f.period_kind = candidate.period_kind
            JOIN filings persisted ON persisted.id = f.filing_id
            WHERE candidate.id != persisted.id
              AND ({_rank_case.replace('doc_type', 'candidate.doc_type')})
                <
                  ({_rank_case.replace('doc_type', 'persisted.doc_type')})
              AND candidate.extracted_utc IS NOT NULL
            """
        ).fetchall()

        counts["periods_shadowed"] = len({
            (r["ticker"], r["period_year"], r["period_kind"]) for r in rows
        })
        counts["filings_reset"] = len(rows)

        if not dry_run and rows:
            ids = [r["filing_id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE filings SET extracted_utc = NULL WHERE id IN ({placeholders})",
                ids,
            )
            conn.commit()

    log.info("fundamentals recover: %s", counts)
    return counts


def get_ownership(ticker: str) -> OwnershipView:
    with connect(settings.db_path) as conn:
        latest = financials_repo.latest_period(conn, ticker)
        if latest is None:
            return OwnershipView(ticker=ticker)
        rows = financials_repo.list_ownership(conn, ticker, int(latest["period_year"]))

    return OwnershipView(
        ticker=ticker,
        period_year=int(latest["period_year"]),
        period_kind=latest["period_kind"],
        holders=[
            {"holder": r["holder"], "share_pct": r["share_pct"], "shares": r["shares"]}
            for r in rows
        ],
    )
