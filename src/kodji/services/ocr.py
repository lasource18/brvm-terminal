"""Scanned-PDF rescue via ocrmypdf (Phase 4c).

Wraps the `ocrmypdf` CLI so `services/fundamentals.extract_pending` can pick
up French annual reports whose original PDF is image-only. The pipeline is:

    filings.is_scanned = 1
       │
       │ (this module)
       ▼
    filings.is_scanned = 0
    filings.extracted_utc = NULL   (re-queued for extraction)
    filings.ocr_attempted_utc set  (never re-tried automatically)

Two invariants keep this cheap and idempotent:

1. **Never re-OCR a filing automatically.** Every filing handed to the
   OCR runner — success or failure — gets `filings.ocr_attempted_utc`
   stamped. An operator can force a retry by clearing that column.
2. **Bounded per-file wall time.** `settings.ocr_timeout_s` caps each
   run; a pathological 500-page scan can't eat the whole night.

The runner is injectable so tests can exercise the whole pipeline without
a real tesseract install.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.store import filings as filings_repo

log = get(__name__)


class OcrUnavailable(RuntimeError):
    """Raised when the ocrmypdf binary isn't installed / on PATH."""


@dataclass(frozen=True)
class OcrOutcome:
    """Result of one OCR attempt. Immutable — the caller writes the DB."""

    ok: bool
    reason: str = ""              # e.g. 'timeout', 'nonzero_exit(2)', 'ok'
    output_size_bytes: int = 0
    output_sha256: str = ""
    output_page_count: int | None = None


# Signature of an injectable OCR runner. Real callers use `run_ocrmypdf`;
# tests provide a stub that writes bytes to `output_path` and returns.
OcrRunner = Callable[[Path, Path], None]


def run_ocrmypdf(input_path: Path, output_path: Path) -> None:
    """Default runner — shells out to the `ocrmypdf` CLI.

    Chosen over the Python API because a subprocess can be killed cleanly
    on timeout without a stray tesseract process lingering."""
    binary = shutil.which(settings.ocr_binary) or settings.ocr_binary
    args = [
        binary,
        "--language", settings.ocr_languages,
        # Existing text-layer pages are left as-is; scans get OCR overlaid.
        "--skip-text",
        # Keep the file small — full optimize is slow, but level 1 is quick
        # and drops ~30% on typical scans without recompressing images.
        "--optimize", "1",
        # Deterministic output — no author/creator metadata bumps.
        "--output-type", "pdf",
        str(input_path),
        str(output_path),
    ]
    try:
        subprocess.run(
            args,
            check=True,
            timeout=settings.ocr_timeout_s,
            capture_output=True,
        )
    except FileNotFoundError as e:
        raise OcrUnavailable(f"ocrmypdf binary not found: {binary}") from e


def _pdf_page_count(path: Path) -> int | None:
    """Same helper shape as services/filings — kept here to avoid a
    cross-module import cycle."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dep is pinned
        return None
    try:
        reader = PdfReader(str(path))
        return len(reader.pages)
    except Exception as e:
        log.warning("pypdf couldn't read %s after OCR: %s", path, e)
        return None


def ocr_file(
    path: Path,
    *,
    runner: OcrRunner | None = None,
) -> OcrOutcome:
    """Run OCR on `path` in place and return the outcome.

    Never raises for the operational cases (timeout, non-zero exit,
    unreadable output): the caller stamps `ocr_attempted_utc` regardless
    so a bad file doesn't re-enter the queue on the next pass. The one
    exception is `OcrUnavailable`, which the caller catches once and
    aborts the whole pass — running the loop with a missing binary would
    just stamp every filing as failed."""
    if not path.exists():
        return OcrOutcome(ok=False, reason=f"missing_file:{path}")

    runner = runner or run_ocrmypdf
    tmp_out = path.with_suffix(path.suffix + ".ocr.pdf")
    try:
        runner(path, tmp_out)
    except subprocess.TimeoutExpired:
        tmp_out.unlink(missing_ok=True)
        return OcrOutcome(ok=False, reason="timeout")
    except subprocess.CalledProcessError as e:
        tmp_out.unlink(missing_ok=True)
        # Ocrmypdf returns 6 for "PriorOcrFoundError" — the input already had
        # text, so we treat it as a soft success (no OCR needed) but flag
        # for the caller so it can flip `is_scanned=0`.
        if e.returncode == 6:
            return OcrOutcome(ok=False, reason="already_ocr")
        return OcrOutcome(ok=False, reason=f"nonzero_exit:{e.returncode}")
    except OcrUnavailable:
        tmp_out.unlink(missing_ok=True)
        raise

    if not tmp_out.exists():
        return OcrOutcome(ok=False, reason="no_output")

    # Success — atomically replace the original with the OCR'd copy.
    size = tmp_out.stat().st_size
    sha = hashlib.sha256(tmp_out.read_bytes()).hexdigest()
    tmp_out.replace(path)
    return OcrOutcome(
        ok=True,
        reason="ok",
        output_size_bytes=size,
        output_sha256=sha,
        output_page_count=_pdf_page_count(path),
    )


# --------------------------------------------------------------------------
# Batch worker (Phase 4c)
# --------------------------------------------------------------------------


@dataclass
class OcrCounts:
    pending_before: int = 0
    considered: int = 0
    ok: int = 0
    already_had_text: int = 0    # ocrmypdf returncode 6 -> flipped to non-scanned
    failed: int = 0
    unavailable: int = 0         # binary missing; whole pass aborts
    missing_file: int = 0
    pending_after: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "pending_before": self.pending_before,
            "considered": self.considered,
            "ok": self.ok,
            "already_had_text": self.already_had_text,
            "failed": self.failed,
            "unavailable": self.unavailable,
            "missing_file": self.missing_file,
            "pending_after": self.pending_after,
        }


def _resolve_path(root: Path, file_path: str) -> Path:
    p = Path(file_path)
    return p if p.is_absolute() else (root / p)


def ocr_pending(
    *,
    limit: int | None = None,
    project_root: Path | None = None,
    runner: OcrRunner | None = None,
) -> OcrCounts:
    """Run OCR over every filing with `is_scanned=1 AND ocr_attempted_utc
    IS NULL`, up to `settings.ocr_max_files_per_run`.

    Degrades quietly: a missing `ocrmypdf` binary sets `unavailable=1`
    and returns without touching any row, so a fresh install without the
    OCR toolchain won't corrupt the DB. Per-file failures stamp
    `ocr_attempted_utc` so a bad scan doesn't re-enter the queue nightly.
    """
    root = project_root or Path.cwd()
    counts = OcrCounts()
    max_files = limit or settings.ocr_max_files_per_run

    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        rows = filings_repo.list_pending_ocr(
            conn, max_pages=settings.ocr_max_pages, limit=max_files
        )
        counts.pending_before = filings_repo.count_pending_ocr(conn)

        if not rows:
            counts.pending_after = counts.pending_before
            log.info("ocr: nothing to do (pending=%d)", counts.pending_before)
            return counts

        for row in rows:
            counts.considered += 1
            path = _resolve_path(root, row["file_path"])
            if not path.exists():
                counts.missing_file += 1
                # Stamp: the file isn't coming back on its own, and we don't
                # want it re-selected every night just to fail the same way.
                filings_repo.apply_ocr_failure(conn, int(row["id"]))
                continue

            try:
                outcome = ocr_file(path, runner=runner)
            except OcrUnavailable as e:
                counts.unavailable = 1
                log.warning("ocr: %s (skipping remaining %d filings)",
                            e, len(rows) - counts.considered)
                break

            if outcome.ok:
                filings_repo.apply_ocr_success(
                    conn,
                    int(row["id"]),
                    size_bytes=outcome.output_size_bytes,
                    sha256=outcome.output_sha256,
                    page_count=outcome.output_page_count,
                )
                counts.ok += 1
                continue

            if outcome.reason == "already_ocr":
                # PDF has a text layer after all — flip is_scanned=0 and
                # clear extracted_utc so it lands back on the extractor's
                # queue. Nothing to rewrite on disk.
                filings_repo.apply_ocr_success(
                    conn,
                    int(row["id"]),
                    size_bytes=int(row["size_bytes"]),
                    sha256=row["sha256"],
                    page_count=row["page_count"],
                )
                counts.already_had_text += 1
                continue

            log.warning(
                "ocr: filing %s failed (%s): %s",
                row["id"], outcome.reason, path,
            )
            filings_repo.apply_ocr_failure(conn, int(row["id"]))
            counts.failed += 1

        counts.pending_after = filings_repo.count_pending_ocr(conn)

    log.info("ocr pass: %s", counts.as_dict())
    return counts
