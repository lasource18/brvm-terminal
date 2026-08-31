"""One-shot news + corporate-actions poller used by `just news-poll`.

Also registered as a scheduled callable (see jobs/scheduler.py).
"""

from __future__ import annotations

import argparse
from datetime import date

from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.services.news import poll_all
from kodji.store import news as news_repo

log = get(__name__)


def _print_summary(counts: dict[str, int]) -> None:
    print("\nnews poll:")
    for k, v in counts.items():
        print(f"  {k:>18} = {v}")

    with connect(settings.db_path) as conn:
        rows = news_repo.list_news(conn, limit=5)
        upcoming = news_repo.list_corporate_actions_upcoming(
            conn, days=30, today=date.today()
        )

    print(f"\nlatest {len(rows)} news items:")
    for r in rows:
        when = (r["published_at"] or r["fetched_utc"] or "")[:16]
        marker = "*" if r["kind"] == "communique" else " "
        print(f"  {marker} {when}  {r['title'][:90]}")

    print(f"\nupcoming corporate actions (next 30d): {len(upcoming)}")
    for r in upcoming[:10]:
        d = r["ex_date"] or "TBD"
        amt = "-" if r["amount"] is None else f"{r['amount']:>10,.2f}"
        y = "-" if r["yield_pct"] is None else f"{r['yield_pct']:>5.2f}%"
        print(f"  {d}  {r['ticker']:<6}  {r['kind']:<9}  amt={amt}  yield={y}")
    print()


def run_once() -> None:
    counts = poll_all()
    _print_summary(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description="kodji-terminal news poll")
    parser.add_argument(
        "--once", action="store_true", help="run one poll cycle then exit"
    )
    parser.parse_args()
    run_once()


if __name__ == "__main__":
    main()
