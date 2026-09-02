"""Fill in missing FR translations on briefs and analyst notes.

Both writers translate at write time and cache the result on the row
(`markdown_fr`), but a row written before PR-I — or one whose
translation call failed softly — keeps a NULL and the UI shows a
"translation pending" badge forever. Nothing re-runs it: the generators
are keyed on `day` / `(ticker, week_start)` and skip rows they have
already written, so the note is never revisited.

This walks those rows and translates them. It is the same Haiku call the
writers make, billed to the same daily counters, so a backfill is
subject to the same `LLM_DAILY_CAP_CENTS` ceiling as a normal run.

    uv run python scripts/backfill_translations.py --dry-run
    uv run python scripts/backfill_translations.py --limit 5
    uv run python scripts/backfill_translations.py --ticker SNTS
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.services import analyst_notes as notes_svc
from kodji.services import brief as brief_svc
from kodji.services import translation as translation_svc
from kodji.store import analyst_notes as notes_repo
from kodji.store import briefs as briefs_repo

log = get(__name__)


def _pending(conn, table: str, cols: str, extra: str = "") -> list:
    return list(
        conn.execute(
            f"SELECT {cols} FROM {table} "
            f"WHERE (markdown_fr IS NULL OR markdown_fr = '') "
            f"AND markdown IS NOT NULL AND markdown != '' {extra} "
            f"ORDER BY 1 DESC"
        )
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be translated, spend nothing")
    ap.add_argument("--limit", type=int, default=0, help="cap rows per table")
    ap.add_argument("--ticker", help="restrict analyst notes to one ticker")
    ap.add_argument("--skip-briefs", action="store_true")
    ap.add_argument("--skip-notes", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not translation_svc.has_llm():
        print("ANTHROPIC_API_KEY is not configured — nothing to do.", file=sys.stderr)
        return 1

    db = Path(settings.db_path)
    today = date.today()
    done = failed = 0

    with connect(db) as conn:
        briefs = [] if args.skip_briefs else _pending(conn, "briefs", "day")
        note_filter = f"AND ticker = '{args.ticker.upper()}'" if args.ticker else ""
        notes = [] if args.skip_notes else _pending(
            conn, "analyst_notes", "week_start, ticker", note_filter
        )

    if args.limit:
        briefs, notes = briefs[: args.limit], notes[: args.limit]

    print(f"pending: {len(briefs)} brief(s), {len(notes)} note(s)")
    if args.dry_run:
        for (day,) in briefs:
            print(f"  brief {day}")
        for week, ticker in notes:
            print(f"  note  {ticker} week {week}")
        return 0

    for (day,) in briefs:
        with connect(db) as conn:
            row = briefs_repo.get(conn, day)
        # Reuse each writer's own helper so the spend lands on the same
        # daily counter a normal run would have billed.
        out = brief_svc.translate_or_none(row.markdown, client=None, day=today)
        if out is None:
            log.warning("brief %s: translation failed", day)
            failed += 1
            continue
        with connect(db) as conn:
            briefs_repo.set_translation(conn, day, out[0])
        print(f"  brief {day}  ✓ ({out[1].output_tokens} out tokens)")
        done += 1

    for week, ticker in notes:
        with connect(db) as conn:
            row = notes_repo.get(conn, ticker, week)
        out = notes_svc.translate_or_none(row.markdown, client=None, day=today)
        if out is None:
            log.warning("note %s %s: translation failed", ticker, week)
            failed += 1
            continue
        with connect(db) as conn:
            notes_repo.set_translation(conn, ticker, week, out[0])
        print(f"  note  {ticker} week {week}  ✓ ({out[1].output_tokens} out tokens)")
        done += 1

    print(f"\ntranslated {done}, failed {failed}")
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
