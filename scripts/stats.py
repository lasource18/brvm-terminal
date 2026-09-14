"""Print the pageview summary. Run with `just stats` (`--days N`).

Reads the first-party counters described in `services/analytics` — no
third-party account to log into, and nothing here left the box.

A caveat worth keeping in mind while reading the output: a visitor is
counted once per day, because the hash that identifies them is salted
per day and the salt is destroyed when the day rolls. So the visitor
counts for two days cannot be added together, and the total shown for a
window is distinct visitors *within* that window, not the sum of its days.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kodji.services import analytics  # noqa: E402


def _cell(value: object) -> str:
    """Render one cell. A real zero prints as `0`, not as the em dash —
    "nobody was signed in" and "we have no figure" are different claims
    and a dash for both makes the table lie quietly.
    """
    return "—" if value is None or value == "" else str(value)


def _table(rows: list[dict], columns: list[tuple[str, str]], empty: str) -> None:
    if not rows:
        print(f"  {empty}")
        return
    widths = {
        key: max(len(header), *(len(_cell(r.get(key))) for r in rows))
        for key, header in columns
    }
    print("  " + "  ".join(h.ljust(widths[k]) for k, h in columns))
    for r in rows:
        print("  " + "  ".join(_cell(r.get(k)).ljust(widths[k]) for k, _ in columns))


def main() -> int:
    ap = argparse.ArgumentParser(description="kodji pageview summary")
    ap.add_argument("--days", type=int, default=14, help="window in days (default 14)")
    args = ap.parse_args()

    s = analytics.summary(days=args.days)

    print(f"\nkodji — {args.days} days since {s.since_day}\n")
    if not s.views:
        if s.suspected_views:
            print(f"  No human pageviews — {s.suspected_views} request(s) looked automated.\n")
            return 0
        print("  No pageviews recorded yet.\n")
        print("  If the app is deployed and being visited, check that the")
        print("  migration ran (0023_analytics) and that you are not the only")
        print("  visitor arriving with a filtered user agent.\n")
        return 0

    views = f"{s.views} view" + ("" if s.views == 1 else "s")
    visitors = f"{s.visitors} visitor" + ("" if s.visitors == 1 else "s")
    print(f"  {views} from {visitors}")
    if s.suspected_views:
        print(
            f"  excluding {s.suspected_views} view"
            + ("" if s.suspected_views == 1 else "s")
            + f" from {s.suspected_visitors} suspected crawler"
            + ("" if s.suspected_visitors == 1 else "s")
        )
    print()

    print("By day")
    _table(
        s.days,
        [("day", "DAY"), ("views", "VIEWS"), ("visitors", "VISITORS"),
         ("signed_in_views", "SIGNED IN"), ("pwa_views", "FROM APP")],
        "nothing yet",
    )

    print("\nTop pages")
    _table(s.paths, [("path", "PATH"), ("views", "VIEWS"), ("visitors", "VISITORS")], "nothing yet")

    print("\nReferrers")
    _table(
        s.referrers,
        [("referrer_host", "HOST"), ("views", "VIEWS"), ("visitors", "VISITORS")],
        "none — all traffic arrived directly or with no referrer",
    )

    print("\nLanguage served")
    _table(s.locales, [("locale", "LOCALE"), ("views", "VIEWS"), ("visitors", "VISITORS")], "nothing yet")

    print("\nFunnel")
    pricing_pct = 100 * s.pricing_visitors / s.visitors if s.visitors else 0
    login_pct = 100 * s.signup_visitors / s.visitors if s.visitors else 0
    print(f"  visitors            {s.visitors}")
    print(f"  reached /pricing    {s.pricing_visitors}  ({pricing_pct:.0f}%)")
    print(f"  reached /login      {s.signup_visitors}  ({login_pct:.0f}%)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
