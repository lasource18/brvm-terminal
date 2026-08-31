"""One-shot OCR job used by `just filings-ocr` (Phase 4c).

Runs ocrmypdf over `filings.is_scanned=1` rows so that
`fundamentals-extract` can pick them up on its next pass. Free — no LLM
call — but bounded by wall-clock (see `settings.ocr_timeout_s` and
`settings.ocr_max_files_per_run`).
"""

from __future__ import annotations

import argparse

from kodji.logging import get
from kodji.services.ocr import ocr_pending

log = get(__name__)


def _print_summary(counts: dict[str, int]) -> None:
    print("\nfilings OCR pass:")
    for k in (
        "pending_before",
        "considered",
        "ok",
        "already_had_text",
        "failed",
        "missing_file",
        "unavailable",
        "pending_after",
    ):
        print(f"  {k:>22} = {counts[k]}")
    if counts.get("unavailable"):
        print("\n  ocrmypdf binary not found — install it (brew install ocrmypdf).")
    print()


def run_once(limit: int | None = None) -> None:
    counts = ocr_pending(limit=limit)
    _print_summary(counts.as_dict())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="kodji-terminal OCR pass over scanned filings"
    )
    parser.add_argument("--once", action="store_true", help="run one pass then exit")
    parser.add_argument(
        "--limit", type=int, default=None,
        help=(
            "cap the number of files this pass will OCR "
            "(default: OCR_MAX_FILES_PER_RUN)"
        ),
    )
    args = parser.parse_args()
    run_once(limit=args.limit)


if __name__ == "__main__":
    main()
