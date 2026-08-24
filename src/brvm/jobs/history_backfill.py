"""One-shot bulk history backfill used by `just history-backfill`.

Populates `daily_bars` for every active equity so the Directory page's
period-return columns (1W / 1M / 3M / YTD / 1Y / ALL%) render values
instead of "—" for tickers a user hasn't personally clicked on.

Idempotent within `--min-age-days` (default 7): a rerun during the
week skips tickers whose newest bar was ingested recently.
"""

from __future__ import annotations

import argparse

from brvm.logging import get
from brvm.services.history import backfill_all

log = get(__name__)


def _print_summary(counts: dict[str, int]) -> None:
    print("\nhistory backfill:")
    for k in ("considered", "fetched", "up_to_date", "no_rows",
              "failed", "bars_inserted"):
        print(f"  {k:>16} = {counts[k]}")
    print()


def run_once(*, min_age_days: int = 7) -> None:
    counts = backfill_all(min_age_days=min_age_days)
    _print_summary(counts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="brvm-terminal history backfill (sikafinance historique per equity)"
    )
    parser.add_argument("--once", action="store_true", help="run one pass then exit")
    parser.add_argument(
        "--min-age-days", type=int, default=7,
        help="refresh tickers whose newest bar is older than this many days (default: 7)",
    )
    args = parser.parse_args()
    run_once(min_age_days=args.min_age_days)


if __name__ == "__main__":
    main()
