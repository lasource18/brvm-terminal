"""One-shot filings poller used by `just filings-pull`.

Walks the brvm.org issuer index, resolves slugs to tickers, and
downloads any new PDFs into `data/filings/<ticker>/`. Phase 4a only —
no LLM calls, no extraction.
"""

from __future__ import annotations

import argparse

from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.services.filings import pull_all
from brvm.store import filings as filings_repo
from brvm.store import slugs as slugs_repo

log = get(__name__)


def _print_summary(counts: dict[str, int]) -> None:
    print("\nfilings pull:")
    for k, v in counts.items():
        print(f"  {k:>28} = {v}")

    with connect(settings.db_path) as conn:
        total = filings_repo.count_all(conn)
        latest = conn.execute(
            "SELECT ticker, doc_type, period_kind, period_year, published_date, "
            "size_bytes, page_count, file_path "
            "FROM filings ORDER BY id DESC LIMIT 10"
        ).fetchall()
        unresolved = slugs_repo.list_unresolved(conn, "brvm_org")

    print(f"\ntotal filings on disk: {total}")
    if latest:
        print("\nlatest 10 filings:")
        for r in latest:
            when = r["published_date"] or "?"
            kb = f"{r['size_bytes'] / 1024:>7.1f}kB"
            pages = "" if r["page_count"] is None else f" {r['page_count']:>3}p"
            period = r["period_kind"] or "-"
            year = r["period_year"] or ""
            print(f"  {when}  {r['ticker']:<6}  {r['doc_type']:<18}"
                  f"  {period:<6}{year!s:<5}  {kb}{pages}")

    if unresolved:
        print(f"\nunresolved brvm.org slugs (add mapping via UPDATE): {len(unresolved)}")
        for r in unresolved[:10]:
            print(f"  {r['slug']:<40}  {r['display_name'] or '-'}")
    print()


def run_once(*, max_issuers: int | None = None) -> None:
    counts = pull_all(max_issuers=max_issuers)
    _print_summary(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description="brvm-terminal filings pull")
    parser.add_argument(
        "--once", action="store_true", help="run one pull cycle then exit"
    )
    parser.add_argument(
        "--max-issuers", type=int, default=None,
        help="cap issuers walked in this pass (useful for smoke runs)",
    )
    args = parser.parse_args()
    run_once(max_issuers=args.max_issuers)


if __name__ == "__main__":
    main()
