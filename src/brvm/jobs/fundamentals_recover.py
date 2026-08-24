"""One-shot recovery for periods shadowed by the pre-PR-#13 replace_period bug.

Clears `extracted_utc` on filings whose extract was overwritten by a
lesser filing (etats_financiers landed after rapport_annuel and wiped
its shareholder register). The next `just fundamentals-extract` run
re-processes them; the preserve-on-empty logic in `replace_period`
(PR #13) then keeps the P&L numbers and re-populates ownership +
segments.

Idempotent — a rerun after `just fundamentals-extract` finds nothing.
"""

from __future__ import annotations

import argparse

from brvm.logging import get
from brvm.services.fundamentals import reset_shadowed_extractions

log = get(__name__)


def _print_summary(counts: dict[str, int]) -> None:
    print("\nfundamentals recover (shadowed periods):")
    for k in ("periods_shadowed", "filings_reset", "dry_run"):
        print(f"  {k:>18} = {counts[k]}")
    if counts["filings_reset"] and not counts["dry_run"]:
        print(
            "\n  Run `just fundamentals-extract` to re-process the reset "
            "filings and re-populate ownership + segments."
        )
    print()


def run_once(*, dry_run: bool = False) -> None:
    counts = reset_shadowed_extractions(dry_run=dry_run)
    _print_summary(counts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="brvm-terminal fundamentals recovery (unshadow periods)"
    )
    parser.add_argument("--once", action="store_true", help="run one pass then exit")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report shadowed counts without touching the DB",
    )
    args = parser.parse_args()
    run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
