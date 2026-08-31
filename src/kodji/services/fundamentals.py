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

from kodji.clock import utcnow
from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.services import extraction
from kodji.store import filings as filings_repo
from kodji.store import financials as financials_repo
from kodji.store import spend as spend_repo

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
    # F-28: prefer the filing's own period_kind (derived deterministically
    # from the filename by `brvm_org_filings._classify_period`) over the
    # model's guess. A mislabelled interim from the LLM (e.g., an H1
    # report the model called "annual") would otherwise full-replace a
    # genuine annual row via `replace_period`. Log a warning when the
    # two disagree so the mismatch is investigatable without noise on
    # every filing.
    filing_kind = row["period_kind"]
    if extract.period_kind and filing_kind and extract.period_kind != filing_kind:
        log.warning(
            "extract period_kind %s disagrees with filing %s for %s "
            "(filing id=%d); trusting filing",
            extract.period_kind, filing_kind, row["ticker"], row["id"],
        )
    period_kind = filing_kind or extract.period_kind or "annual"
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
            extract.cash_flow_ops,
            extract.capex,
            extract.free_cash_flow,
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
            cash_flow_ops=extract.cash_flow_ops,
            capex=extract.capex,
            free_cash_flow=extract.free_cash_flow,
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
    """One P&L / balance-sheet row per period, most recent on the left.

    `currencies` carries the per-period reporting currency (F-24): most
    issuers report in XOF but some publish EUR or USD comparatives. When
    the list contains more than one distinct code the caller must render
    per-column rather than pretend the whole table shares one unit.
    `currency` stays for backwards-compat callers — it holds the
    majority currency when uniform, otherwise the newest row's code.
    """

    ticker: str
    currency: str = "XOF"
    periods: list[int] = field(default_factory=list)                     # descending years
    currencies: list[str] = field(default_factory=list)                  # per-period, same order
    metrics: dict[str, list[float | None]] = field(default_factory=dict)  # metric -> per-period
    source_filings: dict[int, int] = field(default_factory=dict)         # year -> filing_id

    @property
    def has_data(self) -> bool:
        return bool(self.periods)

    @property
    def has_mixed_currencies(self) -> bool:
        return len({c for c in self.currencies if c}) > 1


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
    "cash_flow_ops",
    "capex",
    "free_cash_flow",
)


def get_financials_series(ticker: str, *, limit: int = 6) -> FinancialsSeries:
    """5-year (default) annual series for the Financials tab."""
    with connect(settings.db_path) as conn:
        rows = financials_repo.list_financials(conn, ticker, limit=limit)
    series = FinancialsSeries(ticker=ticker)
    if not rows:
        return series
    series.currencies = [r["currency"] or "XOF" for r in rows]
    # `currency` is the newest row's code — kept for callers that only
    # render one caption. Templates that render per-column should read
    # `currencies` and consult `has_mixed_currencies`.
    series.currency = series.currencies[0]
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
                   eps, dividend_per_share, cash_flow_ops, capex,
                   free_cash_flow, filing_id
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
    """Latest annual period business + geographic split for the Segments tab.

    F-07: prefer the most recent annual period that actually has segment
    rows. Otherwise a fresh statements-only filing (no segments in the
    extract) would hide the prior year's still-persisted segments.
    """
    with connect(settings.db_path) as conn:
        row = conn.execute(
            """
            SELECT f.period_year, f.period_kind, f.currency
            FROM financials f
            WHERE f.ticker = ?
              AND f.period_kind = 'annual'
              AND EXISTS (
                  SELECT 1 FROM financial_segments s
                  WHERE s.ticker      = f.ticker
                    AND s.period_year = f.period_year
                    AND s.period_kind = f.period_kind
              )
            ORDER BY f.period_year DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if row is None:
            return SegmentsView(ticker=ticker)
        rows = financials_repo.list_segments(conn, ticker, int(row["period_year"]))

    view = SegmentsView(
        ticker=ticker,
        period_year=int(row["period_year"]),
        period_kind=row["period_kind"],
        currency=row["currency"] or "XOF",
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


@dataclass(frozen=True)
class FilingRef:
    """One row in the Financials tab's "References" subsection: the
    filing that produced the extract for a given `(period_year,
    period_kind)`. `source_url` links out to the original PDF on the
    issuer / brvm.org / sikafinance so a reader can audit the numbers.
    """

    period_year: int
    period_kind: str
    doc_type: str
    published_date: str | None
    source: str
    source_url: str
    filing_id: int


def get_financials_source_filings(ticker: str, *, limit: int = 24) -> list[FilingRef]:
    """Filings currently backing the persisted `financials` rows for
    `ticker` — one entry per `(period_year, period_kind)` triple, joined
    onto the filings table for the audit-trail metadata.

    The Financials tab renders these as a "References" table so a reader
    can jump from a P&L cell to the source PDF that produced it. Sorted
    newest-first with annual periods preferred inside a tied year (the
    annual is usually what the user came for).
    """
    with connect(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT f.period_year, f.period_kind, ff.doc_type,
                   ff.published_date, ff.source, ff.source_url, ff.id AS filing_id
            FROM financials f
            JOIN filings ff ON ff.id = f.filing_id
            WHERE f.ticker = ?
            ORDER BY f.period_year DESC,
                     CASE f.period_kind
                        WHEN 'annual' THEN 5
                        WHEN 'Q3'     THEN 4
                        WHEN 'H1'     THEN 3
                        WHEN 'Q1'     THEN 2
                        ELSE 1 END DESC,
                     COALESCE(ff.published_date, ff.fetched_utc) DESC
            LIMIT ?
            """,
            (ticker, limit),
        ).fetchall()
    return [
        FilingRef(
            period_year=int(r["period_year"]),
            period_kind=r["period_kind"],
            doc_type=r["doc_type"],
            published_date=r["published_date"],
            source=r["source"],
            source_url=r["source_url"],
            filing_id=int(r["filing_id"]),
        )
        for r in rows
    ]


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


def reset_missing_cashflow(*, dry_run: bool = False) -> dict[str, int]:
    """Clear `filings.extracted_utc` on filings whose `financials` row
    is missing every cash-flow column, so the next extraction pass
    re-processes them with the Phase-7-aware prompt.

    Only the filing currently backing the persisted `financials` row is
    reset — no attempt to reprocess sibling filings for the same period,
    because `replace_period`'s preserve-on-empty rule already handles
    a second pass composing with the first. Runs against annual rows
    only (interims rarely publish a cash-flow statement).

    F-25 gate: each filing is stamped with
    `cashflow_recovery_attempted_utc` when it is queued, and filings
    with that stamp are skipped on subsequent runs. Without this a
    filing whose cash-flow statement sits past the 120k-char truncation
    (or doesn't exist) would re-extract to NULL and be re-billed
    ~30-50k tokens on every recovery pass. Operators who upgrade the
    prompt can clear the column manually to force a fresh batch.
    """
    counts = {"filings_reset": 0, "dry_run": int(dry_run)}
    with connect(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT f.filing_id
            FROM financials f
            JOIN filings ff ON ff.id = f.filing_id
            WHERE f.period_kind = 'annual'
              AND f.cash_flow_ops IS NULL
              AND f.capex IS NULL
              AND f.free_cash_flow IS NULL
              AND ff.extracted_utc IS NOT NULL
              AND (ff.is_scanned IS NULL OR ff.is_scanned = 0)
              AND ff.cashflow_recovery_attempted_utc IS NULL
            """
        ).fetchall()
        counts["filings_reset"] = len(rows)
        if not dry_run and rows:
            from kodji.clock import utc_iso

            ids = [int(r["filing_id"]) for r in rows]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"""
                UPDATE filings
                SET extracted_utc = NULL,
                    cashflow_recovery_attempted_utc = ?
                WHERE id IN ({placeholders})
                """,
                (utc_iso(), *ids),
            )
            conn.commit()
    log.info("fundamentals recover (cash-flow): %s", counts)
    return counts


def get_ownership(ticker: str) -> OwnershipView:
    """Latest annual period shareholder register for the Ownership tab.

    F-07: prefer the most recent annual period that actually has
    ownership rows. Otherwise a fresh statements-only filing (no
    shareholder register in the extract) would hide the prior year's
    still-persisted holders.
    """
    with connect(settings.db_path) as conn:
        row = conn.execute(
            """
            SELECT f.period_year, f.period_kind
            FROM financials f
            WHERE f.ticker = ?
              AND f.period_kind = 'annual'
              AND EXISTS (
                  SELECT 1 FROM ownership o
                  WHERE o.ticker      = f.ticker
                    AND o.period_year = f.period_year
                    AND o.period_kind = f.period_kind
              )
            ORDER BY f.period_year DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if row is None:
            return OwnershipView(ticker=ticker)
        rows = financials_repo.list_ownership(conn, ticker, int(row["period_year"]))

    return OwnershipView(
        ticker=ticker,
        period_year=int(row["period_year"]),
        period_kind=row["period_kind"],
        holders=[
            {"holder": r["holder"], "share_pct": r["share_pct"], "shares": r["shares"]}
            for r in rows
        ],
    )
