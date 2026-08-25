"""One-shot brief writer used by `just brief-run`.

Also registered as a scheduled callable (see jobs/scheduler.py) for the
post-close job at 15:30 Africa/Abidjan Mon-Fri.
"""

from __future__ import annotations

import argparse
from datetime import date

from brvm.logging import get
from brvm.services.brief import BriefResult, generate_for

log = get(__name__)


def _print_summary(result: BriefResult) -> None:
    d = result.as_dict()
    print("\nbrief run:")
    for k, v in d.items():
        print(f"  {k:>18} = {v}")
    if result.brief:
        title = result.brief.title or "(untitled)"
        print(f"\ntitle: {title}\n")
        head = result.brief.markdown.splitlines()[:4]
        for line in head:
            print(f"  {line}")
        if len(result.brief.markdown.splitlines()) > 4:
            print("  ...")
    print()


def run_once(*, dry_run: bool = False, day: date | None = None) -> None:
    result = generate_for(day=day, dry_run=dry_run)
    _print_summary(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="brvm-terminal daily brief")
    parser.add_argument("--once", action="store_true", help="run one pass then exit")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="assemble context + print the plan, spend nothing",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="ISO date the brief should cover (default: today UTC)",
    )
    args = parser.parse_args()
    day = date.fromisoformat(args.date) if args.date else None
    run_once(dry_run=args.dry_run, day=day)


if __name__ == "__main__":
    main()
