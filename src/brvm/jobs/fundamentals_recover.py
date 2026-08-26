"""One-shot recovery jobs for the fundamentals extraction pipeline.

Two modes, both idempotent:

* **shadowed** (default): clears `extracted_utc` on filings whose
  extract was overwritten by a lesser filing (etats_financiers landed
  after rapport_annuel and wiped its shareholder register, pre-PR-#13).
  The next `just fundamentals-extract` re-processes them; the
  preserve-on-empty logic in `replace_period` (PR #13) keeps the P&L
  numbers and re-populates ownership + segments.

* **cash-flow** (Phase 7): clears `extracted_utc` on filings whose
  persisted `financials` row is missing every cash-flow column, so the
  next extraction pass runs the Phase-7-aware prompt against them and
  fills in `cash_flow_ops`, `capex`, and `free_cash_flow`. Only annual
  rows are targeted — interim reports rarely publish a cash-flow
  statement.
"""

from __future__ import annotations

import argparse

from brvm.logging import get
from brvm.services.fundamentals import (
    reset_missing_cashflow,
    reset_shadowed_extractions,
)

log = get(__name__)


def _print_shadowed_summary(counts: dict[str, int]) -> None:
    print("\nfundamentals recover (shadowed periods):")
    for k in ("periods_shadowed", "filings_reset", "dry_run"):
        print(f"  {k:>18} = {counts[k]}")
    if counts["filings_reset"] and not counts["dry_run"]:
        print(
            "\n  Run `just fundamentals-extract` to re-process the reset "
            "filings and re-populate ownership + segments."
        )
    print()


def _print_cashflow_summary(counts: dict[str, int]) -> None:
    print("\nfundamentals recover (missing cash-flow columns):")
    for k in ("filings_reset", "dry_run"):
        print(f"  {k:>18} = {counts[k]}")
    if counts["filings_reset"] and not counts["dry_run"]:
        print(
            "\n  Run `just fundamentals-extract` to re-process the reset "
            "filings and populate cash_flow_ops / capex / free_cash_flow."
        )
    print()


def run_once(*, dry_run: bool = False, cash_flow: bool = False) -> None:
    if cash_flow:
        counts = reset_missing_cashflow(dry_run=dry_run)
        _print_cashflow_summary(counts)
    else:
        counts = reset_shadowed_extractions(dry_run=dry_run)
        _print_shadowed_summary(counts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="brvm-terminal fundamentals recovery"
    )
    parser.add_argument("--once", action="store_true", help="run one pass then exit")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report counts without touching the DB",
    )
    parser.add_argument(
        "--cash-flow", action="store_true",
        help=(
            "reset filings whose extract is missing the Phase-7 cash-flow "
            "columns (default: reset shadowed periods)"
        ),
    )
    args = parser.parse_args()
    run_once(dry_run=args.dry_run, cash_flow=args.cash_flow)


if __name__ == "__main__":
    main()
