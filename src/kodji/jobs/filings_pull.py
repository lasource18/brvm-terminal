"""One-shot filings poller used by `just filings-pull`.

Walks the brvm.org issuer index, resolves slugs to tickers, and
downloads any new PDFs into `data/filings/<ticker>/`. Phase 4a only —
no LLM calls, no extraction.
"""

from __future__ import annotations

import argparse

from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.services.filings import promote_from_communiques, pull_all
from kodji.store import filings as filings_repo
from kodji.store import slugs as slugs_repo

log = get(__name__)


def _print_summary(counts: dict[str, int], header: str = "filings pull") -> None:
    print(f"\n{header}:")
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


def run_once(
    *,
    max_issuers: int | None = None,
    only_tickers: set[str] | None = None,
    skip_promote: bool = False,
) -> None:
    counts = pull_all(max_issuers=max_issuers, only_tickers=only_tickers)
    _print_summary(counts)
    # The sikafinance promotion step ingests communiqué PDFs across
    # every ticker — running it under an `only_tickers` filter would be
    # confusing (the summary would still show unrelated tickers), so
    # skip it whenever the caller asked for a per-ticker run.
    if not skip_promote and only_tickers is None:
        promote_counts = promote_from_communiques()
        _print_summary(promote_counts, header="filings promote (sikafinance)")


def main() -> None:
    parser = argparse.ArgumentParser(description="kodji-terminal filings pull")
    parser.add_argument(
        "--once", action="store_true", help="run one pull cycle then exit"
    )
    parser.add_argument(
        "--max-issuers", type=int, default=None,
        help="cap issuers walked in this pass (useful for smoke runs)",
    )
    parser.add_argument(
        "--only-tickers", default=None,
        help="comma-separated ticker allow-list (e.g. 'SNTS' or 'SNTS,ORAC'); "
             "the walk still visits every issuer on the index but only "
             "resolved tickers in this set are fetched. Implies --skip-promote.",
    )
    parser.add_argument(
        "--skip-promote", action="store_true",
        help="skip the sikafinance-communiqué promotion step",
    )
    args = parser.parse_args()
    only_tickers: set[str] | None = None
    if args.only_tickers:
        only_tickers = {
            t.strip().upper() for t in args.only_tickers.split(",") if t.strip()
        } or None
    run_once(
        max_issuers=args.max_issuers,
        only_tickers=only_tickers,
        skip_promote=args.skip_promote,
    )


if __name__ == "__main__":
    main()
