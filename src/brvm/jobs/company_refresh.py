"""One-shot company-facts refresh used by `just company-refresh` (Phase 4d).

Pulls shares_outstanding / float_pct / market_cap for every stale equity
so the ratios engine has fresh inputs to work with.
"""

from __future__ import annotations

import argparse

from brvm.logging import get
from brvm.services.company_facts import refresh_all

log = get(__name__)


def _print_summary(counts: dict[str, int]) -> None:
    print("\ncompany-facts refresh:")
    for k in ("considered", "refreshed", "no_data", "failed"):
        print(f"  {k:>16} = {counts[k]}")
    print()


def run_once(*, max_age_days: int = 7, limit: int = 200) -> None:
    counts = refresh_all(max_age_days=max_age_days, limit=limit)
    _print_summary(counts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="brvm-terminal company-facts refresh (weekly-ish)"
    )
    parser.add_argument("--once", action="store_true", help="run one pass then exit")
    parser.add_argument(
        "--max-age-days", type=int, default=7,
        help="refresh rows older than this many days (default: 7)",
    )
    parser.add_argument(
        "--limit", type=int, default=200,
        help="cap tickers processed this pass (default: 200)",
    )
    args = parser.parse_args()
    run_once(max_age_days=args.max_age_days, limit=args.limit)


if __name__ == "__main__":
    main()
