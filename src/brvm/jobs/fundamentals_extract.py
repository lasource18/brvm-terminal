"""One-shot fundamentals extraction job used by `just fundamentals-extract`.

Also registered as a scheduled callable (see jobs/scheduler.py). Prints
a per-pass summary that mirrors the shape of `just news-tag`."""

from __future__ import annotations

import argparse

from brvm.clock import utcnow
from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.services.fundamentals import extract_pending
from brvm.store import spend as spend_repo

log = get(__name__)


def _usd(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}"


def _print_summary(counts: dict[str, int]) -> None:
    print("\nfundamentals extraction:")
    for k in (
        "pending_before",
        "considered",
        "extracted",
        "empty_payloads",
        "scanned",
        "failed",
        "skipped_budget",
        "skipped_missing_file",
        "pending_after",
    ):
        print(f"  {k:>22} = {counts[k]}")

    spent = counts["spend_micros_after"] - counts["spend_micros_before"]
    cap_micros = settings.llm_extract_daily_cap_cents * 10_000
    print(f"  {'cost this run':>22} = {_usd(spent)}")
    print(
        f"  {'spend today':>22} = "
        f"{_usd(counts['spend_micros_after'])} / {_usd(cap_micros)} cap"
    )
    if counts["llm_disabled"]:
        print("\n  ANTHROPIC_API_KEY is not set — nothing was extracted.")
    if counts["skipped_budget"]:
        print("\n  Daily cap reached; remaining filings resume after UTC midnight.")

    with connect(settings.db_path) as conn:
        day = spend_repo.get_day(conn, utcnow().date(), table="filings_spend")
    if day:
        print(
            f"\nfilings_spend {day['day']}: calls={day['calls']} "
            f"in={day['input_tokens']} out={day['output_tokens']} "
            f"({_usd(day['usd_micros'])})"
        )
    print()


def run_once(limit: int | None = None, dry_run: bool = False) -> None:
    counts = extract_pending(limit=limit, dry_run=dry_run)
    _print_summary(counts.as_dict())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="brvm-terminal fundamentals extraction (Haiku over PDF filings)"
    )
    parser.add_argument("--once", action="store_true", help="run one pass then exit")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap filings considered per pass (default: 200; budget cap still applies)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run pre-flight only (no API calls, no writes)",
    )
    args = parser.parse_args()
    run_once(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
