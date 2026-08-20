"""One-shot snapshot job used by `just snapshot`.

Also usable as a scheduled callable (see jobs/scheduler.py).
"""

from __future__ import annotations

import argparse

from brvm.logging import get
from brvm.services.quotes import snapshot_once, top_by_turnover

log = get(__name__)


def _fmt_int(n: int | None) -> str:
    return "-" if n is None else f"{n:>12,}"


def _fmt_float(x: float | None, decimals: int = 2) -> str:
    return "-" if x is None else f"{x:>12,.{decimals}f}"


def _print_top() -> None:
    rows = top_by_turnover(limit=10)
    header = f"{'TICKER':<8} {'NAME':<32} {'LAST':>12} {'CHG%':>8} {'VOLUME':>12} {'TURNOVER XOF':>16}"
    print("\n" + header)
    print("-" * len(header))
    for r in rows:
        name = (r["name"] or "")[:32]
        chg = "-" if r["change_pct"] is None else f"{r['change_pct']:+7.2f}%"
        print(
            f"{r['ticker']:<8} {name:<32} {_fmt_float(r['last'])} {chg:>8} "
            f"{_fmt_int(r['volume'])} {_fmt_float(r['turnover'], 0):>16}"
        )
    print()


def run_once() -> None:
    counts = snapshot_once()
    log.info(
        "snapshot wrote: securities=%d snapshots=%d indices=%d",
        counts["securities"],
        counts["snapshots"],
        counts["indices"],
    )
    _print_top()


def main() -> None:
    parser = argparse.ArgumentParser(description="brvm-terminal quote snapshot")
    parser.add_argument("--once", action="store_true", help="run one snapshot cycle then exit")
    parser.parse_args()
    run_once()


if __name__ == "__main__":
    main()
