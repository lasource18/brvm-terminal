"""One-shot news tagging job used by `just news-tag`.

Also registered as a scheduled callable (see jobs/scheduler.py).
"""

from __future__ import annotations

import argparse

from kodji.clock import utcnow
from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.services.tagging import tag_pending
from kodji.store import news as news_repo
from kodji.store import spend as spend_repo

log = get(__name__)


def _usd(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}"


def _print_summary(counts: dict[str, int]) -> None:
    print("\nnews tagging:")
    for k in (
        "pending_before",
        "batches",
        "tagged",
        "unanswered",
        "failed_batches",
        "skipped_budget",
        "pending_after",
    ):
        print(f"  {k:>15} = {counts[k]}")

    spent = counts["spend_micros_after"] - counts["spend_micros_before"]
    cap_micros = settings.llm_daily_cap_cents * 10_000
    print(f"  {'cost this run':>15} = {_usd(spent)}")
    print(f"  {'spend today':>15} = {_usd(counts['spend_micros_after'])} / {_usd(cap_micros)} cap")
    if counts["llm_disabled"]:
        print("\n  ANTHROPIC_API_KEY is not set — nothing was tagged.")
    if counts["skipped_budget"]:
        print("\n  Daily cap reached; the rest resumes after UTC midnight.")

    with connect(settings.db_path) as conn:
        rows = news_repo.list_news(conn, limit=8)
        day = spend_repo.get_day(conn, utcnow().date())

    if day:
        print(
            f"\nllm_spend {day['day']}: calls={day['calls']} "
            f"in={day['input_tokens']} out={day['output_tokens']} "
            f"({_usd(day['usd_micros'])})"
        )

    print(f"\nlatest {len(rows)} news items:")
    for r in rows:
        rel = "  -" if r["relevance"] is None else f"{r['relevance']:>3}"
        cat = (r["category_llm"] or "-")[:14]
        tks = (r["tickers_llm"] or "-")[:18]
        print(f"  rel={rel}  {cat:<14}  {tks:<18}  {r['title'][:60]}")
        if r["summary_en"]:
            print(f"           {r['summary_en'][:96]}")
    print()


def run_once(limit: int | None = None, dry_run: bool = False) -> None:
    counts = tag_pending(limit=limit, dry_run=dry_run)
    _print_summary(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description="kodji-terminal news tagging (Haiku)")
    parser.add_argument("--once", action="store_true", help="run one tagging pass then exit")
    parser.add_argument("--limit", type=int, default=None, help="cap items processed")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be sent without calling the API",
    )
    args = parser.parse_args()
    run_once(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
