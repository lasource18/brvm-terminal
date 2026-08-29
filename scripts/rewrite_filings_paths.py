"""One-shot: rewrite absolute `filings.file_path` rows to their
project-relative form.

F-23 background: pre-2026-09 ingest stored `str(dest)` where `dest`
was the absolutized `FILINGS_ROOT / ticker / file_name`. Every row
carried a machine-local absolute path, so moving the corpus + DB
from Mac to the Hetzner VPS (the charter's deployment target) broke
every extract / OCR pass silently — `_resolve_path` returned the
still-absolute Mac path, `Path.exists()` was False, and the file
was flagged `skipped_missing_file` forever without re-downloading
(the URL hash was already known).

Run this once on a live DB after upgrading to a build that stores
relative paths going forward. Idempotent: rows already relative or
absolute-outside-the-project are left alone.

Usage:
  uv run python scripts/rewrite_filings_paths.py           # dry run
  uv run python scripts/rewrite_filings_paths.py --apply   # writes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from brvm.config import settings  # noqa: E402
from brvm.db import connect  # noqa: E402


def run(*, apply: bool) -> int:
    db_path = Path(settings.db_path)
    print(f"[rewrite-paths] db={db_path}  project_root={ROOT}")

    rewrote = 0
    already = 0
    outside = 0
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, file_path FROM filings WHERE file_path IS NOT NULL"
        ).fetchall()
        print(f"[rewrite-paths] {len(rows)} row(s) to inspect")
        for r in rows:
            raw = r["file_path"]
            p = Path(raw)
            if not p.is_absolute():
                already += 1
                continue
            try:
                rel = str(p.relative_to(ROOT))
            except ValueError:
                # Corpus lives outside the project root (an absolute
                # FILINGS_ROOT under /mnt or ~). Nothing safe to
                # strip; the reader already handles this case.
                outside += 1
                continue
            if apply:
                conn.execute(
                    "UPDATE filings SET file_path = ? WHERE id = ?",
                    (rel, r["id"]),
                )
            rewrote += 1
        if apply:
            conn.commit()

    print(
        f"[rewrite-paths] rewrote={rewrote} already_relative={already} "
        f"outside_project={outside} apply={apply}"
    )
    return rewrote


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="commit the rewrites (default: dry run)")
    args = p.parse_args()
    run(apply=args.apply)


if __name__ == "__main__":
    main()
