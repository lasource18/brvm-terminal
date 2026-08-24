"""One-shot delivery worker for `just alerts-deliver`.

Drains the `alert_events` queue via the configured Discord webhook.
No-ops (with a warning) when DISCORD_WEBHOOK_URL is unset — events pile
up in the DB with `delivered_utc IS NULL` and `delivery_status='skipped'`,
which is fine for a fresh install without notifications.
"""

from __future__ import annotations

import argparse

from brvm.logging import get
from brvm.services.alerts import deliver_pending

log = get(__name__)


def _print_summary(counts) -> None:  # counts: DeliveryCounts
    d = counts.as_dict()
    print("\nalerts deliver:")
    for k, v in d.items():
        print(f"  {k:>18} = {v}")
    print()


def run_once() -> None:
    counts = deliver_pending()
    _print_summary(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description="brvm-terminal alerts deliver")
    parser.add_argument("--once", action="store_true", help="run one pass then exit")
    parser.parse_args()
    run_once()


if __name__ == "__main__":
    main()
