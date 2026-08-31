"""One-shot alerts evaluator used by `just alerts-eval`.

Walks every enabled rule, fires matching events into `alert_events`.
Also registered as a scheduled callable (see jobs/scheduler.py). The
delivery worker (`just alerts-deliver`) drains the resulting queue.
"""

from __future__ import annotations

import argparse

from kodji.logging import get
from kodji.services.alerts import evaluate_all, list_recent_events

log = get(__name__)


def _print_summary(counts) -> None:  # counts: EvalCounts
    d = counts.as_dict()
    print("\nalerts eval:")
    for k, v in d.items():
        print(f"  {k:>20} = {v}")

    recent = list_recent_events(limit=10)
    print(f"\nrecent events ({len(recent)}):")
    for e in recent:
        when = (e.fired_utc or "")[:16]
        status = e.delivery_status or "queued"
        print(f"  {when}  [{e.kind:<11}] {e.subject[:80]}  ({status})")
    print()


def run_once() -> None:
    counts = evaluate_all()
    _print_summary(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description="kodji-terminal alerts evaluator")
    parser.add_argument("--once", action="store_true", help="run one eval then exit")
    parser.parse_args()
    run_once()


if __name__ == "__main__":
    main()
