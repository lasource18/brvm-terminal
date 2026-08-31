"""One-shot bond snapshot job used by `just bonds-poll`.

Also usable as a scheduled callable (see jobs/scheduler.py).
"""

from __future__ import annotations

import argparse

from kodji.logging import get
from kodji.services.quotes import snapshot_bonds_once

log = get(__name__)


def run_once() -> None:
    counts = snapshot_bonds_once()
    log.info(
        "bond snapshot wrote: securities=%d bars=%d snapshots=%d",
        counts["securities"],
        counts["bars"],
        counts["snapshots"],
    )
    print(
        f"bonds: securities={counts['securities']} bars={counts['bars']} "
        f"snapshots={counts['snapshots']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="kodji-terminal bond snapshot")
    parser.add_argument("--once", action="store_true", help="run one snapshot cycle then exit")
    parser.parse_args()
    run_once()


if __name__ == "__main__":
    main()
