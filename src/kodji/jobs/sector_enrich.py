"""One-shot sector enrichment job used by `just sector-enrich`.

Also registered as a scheduled callable (see jobs/scheduler.py).
"""

from __future__ import annotations

import argparse

from kodji.logging import get
from kodji.services.enrichment import enrich_sectors

log = get(__name__)


def run_once(limit: int | None = None) -> None:
    counts = enrich_sectors(limit=limit)
    log.info(
        "sector enrichment: candidates=%d updated=%d still_missing=%d",
        counts["candidates"],
        counts["updated"],
        counts["still_missing"],
    )
    print(
        f"\nsector enrichment: candidates={counts['candidates']} "
        f"updated={counts['updated']} still_missing={counts['still_missing']}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="kodji-terminal sector enrichment")
    parser.add_argument("--once", action="store_true", help="run one enrichment pass")
    parser.add_argument("--limit", type=int, default=None, help="cap number of equities")
    args = parser.parse_args()
    run_once(limit=args.limit)


if __name__ == "__main__":
    main()
