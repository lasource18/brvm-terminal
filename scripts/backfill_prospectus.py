"""Walk the brvm.org avis feed and pin each bond ticker's admission
PDF onto `securities.prospectus_url`.

Dev/ops tool — hits the network, meant to be run ad-hoc when we want
to (re)seed the column. Idempotent: skips tickers that already carry
a URL unless `--overwrite` is passed. Stops walking pages once every
active bond has a pinned URL or `--max-pages` is exhausted, whichever
comes first.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from brvm.config import settings  # noqa: E402
from brvm.db import connect  # noqa: E402
from brvm.services import bonds as bonds_svc  # noqa: E402
from brvm.sources import brvm_org_avis as avis_src  # noqa: E402
from brvm.sources._http import make_client  # noqa: E402


def _bond_tickers_needing_url(conn, overwrite: bool) -> set[str]:
    if overwrite:
        rows = conn.execute(
            "SELECT ticker FROM securities WHERE kind = 'bond' AND active = 1"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ticker FROM securities WHERE kind = 'bond' AND active = 1 "
            "AND prospectus_url IS NULL"
        ).fetchall()
    return {r["ticker"] for r in rows}


def run(*, max_pages: int, overwrite: bool, sleep_s: float) -> int:
    db_path = Path(settings.db_path)
    print(f"[backfill] db={db_path}")

    with connect(db_path) as conn:
        pending = _bond_tickers_needing_url(conn, overwrite)
        if not pending:
            print("[backfill] nothing to do — every bond already has a URL")
            return 0
        print(f"[backfill] {len(pending)} bond ticker(s) pending")

        client = make_client()
        try:
            total_pinned = 0
            for page in range(max_pages):
                rows, last = avis_src.fetch_avis_page(page, client=client)
                admissions = [r for r in rows if r.is_admission]
                pinned = bonds_svc.pin_prospectus_urls(
                    conn, admissions, overwrite=overwrite
                )
                total_pinned += pinned
                # Refresh the pending set — every pinned ticker leaves it.
                remaining = _bond_tickers_needing_url(conn, overwrite)
                print(
                    f"[backfill] page={page}  admissions={len(admissions)}  "
                    f"pinned+={pinned}  remaining={len(remaining)}"
                )
                if not remaining:
                    print("[backfill] all bonds pinned — stopping")
                    break
                if last is not None and page >= last:
                    print(f"[backfill] reached last page ({last}) — stopping")
                    break
                if sleep_s:
                    time.sleep(sleep_s)
        finally:
            client.close()

    print(f"[backfill] done — {total_pinned} row(s) pinned in total")
    return total_pinned


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--max-pages", type=int, default=40,
                   help="cap on avis pages walked (default: 40)")
    p.add_argument("--overwrite", action="store_true",
                   help="repin even tickers that already have a URL")
    p.add_argument("--sleep", type=float, default=1.0,
                   help="polite pause between page fetches, seconds")
    args = p.parse_args()
    run(max_pages=args.max_pages, overwrite=args.overwrite, sleep_s=args.sleep)


if __name__ == "__main__":
    main()
