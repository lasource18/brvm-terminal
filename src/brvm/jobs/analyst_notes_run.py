"""One-shot analyst-notes writer used by `just analyst-notes-run`.

Also registered as a scheduled callable (see jobs/scheduler.py) for the
weekly Saturday-evening pass at 20:00 Africa/Abidjan.
"""

from __future__ import annotations

import argparse
from datetime import date

from brvm.logging import get
from brvm.services.analyst_notes import (
    PassCounts,
    TickerResult,
    generate_for_all,
    generate_for_ticker,
    iso_week_monday,
)

log = get(__name__)


def _print_all_summary(counts: PassCounts) -> None:
    d = counts.as_dict()
    print("\nanalyst-notes pass:")
    for k, v in d.items():
        print(f"  {k:>18} = {v}")
    if counts.tickers_generated:
        print("\ngenerated:", " ".join(counts.tickers_generated))
    if counts.tickers_failed:
        print("failed:   ", " ".join(counts.tickers_failed))
    print()


def _print_one_summary(result: TickerResult) -> None:
    d = result.as_short_dict()
    print("\nanalyst-note:")
    for k, v in d.items():
        print(f"  {k:>18} = {v}")
    if result.note:
        head = result.note.markdown.splitlines()[:4]
        title = result.note.title or "(untitled)"
        print(f"\ntitle: {title}\n")
        for line in head:
            print(f"  {line}")
        if len(result.note.markdown.splitlines()) > 4:
            print("  ...")
    print()


def run_once(
    *,
    dry_run: bool = False,
    week: date | None = None,
    ticker: str | None = None,
    limit: int | None = None,
) -> None:
    week = week or iso_week_monday()
    if ticker:
        result = generate_for_ticker(ticker, week_start=week, dry_run=dry_run)
        _print_one_summary(result)
        return
    counts = generate_for_all(week_start=week, dry_run=dry_run, limit=limit)
    _print_all_summary(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description="brvm-terminal analyst notes")
    parser.add_argument("--once", action="store_true", help="run one pass then exit")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="assemble context per ticker + print the plan, spend nothing",
    )
    parser.add_argument(
        "--week", type=str, default=None,
        help="ISO date inside the week to cover (default: today UTC)",
    )
    parser.add_argument(
        "--ticker", type=str, default=None,
        help="Restrict the run to one ticker",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap the number of tickers processed (smoke runs)",
    )
    args = parser.parse_args()

    week: date | None = None
    if args.week:
        # Any day in the target ISO week is fine — the store keys on the
        # week's Monday, computed by iso_week_monday().
        week = iso_week_monday(date.fromisoformat(args.week))

    run_once(dry_run=args.dry_run, week=week, ticker=args.ticker, limit=args.limit)


if __name__ == "__main__":
    main()
