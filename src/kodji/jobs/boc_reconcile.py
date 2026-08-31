"""F-04: one-shot BOC reconciliation job.

Used by `just boc-reconcile` for on-demand checks and by the daily
16:00 Abidjan scheduler job (`jobs/scheduler.py`). Cross-checks
`daily_bars.close` against the equity-market table in that day's
official BOC PDF, using the PDF's own session date so the comparison
lands on the right day rather than the newest weekly-backfill row.
"""

from __future__ import annotations

import argparse

from kodji.logging import get
from kodji.services.reconcile import check_boc_close

log = get(__name__)


def run_once(tolerance_pct: float = 0.01) -> None:
    report = check_boc_close(tolerance_pct=tolerance_pct)
    log.info(
        "boc reconcile: session=%s boc_rows=%d matched=%d drift=%d",
        report.session_date, report.boc_rows, report.matched, len(report.drift),
    )
    print(
        f"session={report.session_date} boc_rows={report.boc_rows} "
        f"matched={report.matched} drift={len(report.drift)}"
    )
    for d in report.drift:
        pct = f"{d.delta_pct:+.2f}%" if d.delta_pct is not None else "—"
        local = f"{d.local_close:,.2f}" if d.local_close is not None else "missing"
        print(f"  {d.ticker:<6} boc={d.boc_close:>12,.2f}  local={local:>12}  Δ={pct}")


def main() -> None:
    parser = argparse.ArgumentParser(description="kodji-terminal BOC reconciliation")
    parser.add_argument("--once", action="store_true",
                        help="run one reconciliation cycle then exit")
    parser.add_argument("--tolerance-pct", type=float, default=0.01,
                        help="delta%% threshold for flagging drift (default 0.01)")
    args = parser.parse_args()
    run_once(tolerance_pct=args.tolerance_pct)


if __name__ == "__main__":
    main()
