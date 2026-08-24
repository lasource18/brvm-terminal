"""Phase 4c: OCR pipeline over scanned filings.

The runner is fully stubbed here — no real ocrmypdf/tesseract needed. What
we're pinning is the invariants:

* Every filing handed to the runner (ok, fail, or already-had-text) gets
  `ocr_attempted_utc` stamped so it can't re-enter the queue nightly.
* Success flips `is_scanned=0` and clears `extracted_utc` so the row lands
  back on the extractor's queue on the next pass.
* Failure leaves `is_scanned=1` (the file might work after a tesseract
  upgrade, once the operator clears the attempt stamp manually).
* A missing binary aborts the pass without touching any row.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

from brvm.db import connect
from brvm.models import Filing, Security
from brvm.store import filings as filings_repo
from brvm.store import securities as sec_repo

from .conftest import apply_migrations


def _fresh(monkeypatch, tmp_path):
    db_path = tmp_path / "brvm.sqlite"
    filings_dir = tmp_path / "filings"
    filings_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("FILINGS_ROOT", str(filings_dir))

    import brvm.config as cfg
    import brvm.services.ocr as ocr_svc

    importlib.reload(cfg)
    importlib.reload(ocr_svc)
    return ocr_svc, db_path, filings_dir


def _seed_scanned_filing(
    db_path: Path,
    filings_dir: Path,
    *,
    ticker: str = "SNTS",
    fname: str = "scanned.pdf",
    body: bytes = b"scanned original",
    page_count: int = 40,
    migrate: bool = True,
) -> Path:
    file_path = filings_dir / ticker / fname
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(body)

    url = f"https://example.org/{ticker}/{fname}"
    with connect(db_path) as conn:
        if migrate:
            apply_migrations(conn)
        sec_repo.upsert(conn, [Security(ticker=ticker, name=ticker, kind="equity", country="SN")])
        filings_repo.upsert_filings(conn, [Filing(
            ticker=ticker,
            issuer_name=ticker,
            doc_type="rapport_annuel",
            period_kind="annual",
            period_year=2024,
            source="brvm_org",
            source_url=url,
            url_hash=f"hash-{ticker}-{fname}",
            file_path=str(file_path),
            size_bytes=len(body),
            sha256="original-sha",
            page_count=page_count,
        )])
        conn.execute(
            "UPDATE filings SET is_scanned = 1, extracted_utc = '2026-08-22T03:00:00Z' "
            f"WHERE url_hash = 'hash-{ticker}-{fname}'"
        )
        conn.commit()
    return file_path


def _make_runner(new_bytes: bytes):
    """Writes `new_bytes` to the output path and returns success."""

    def _runner(inp: Path, out: Path) -> None:
        out.write_bytes(new_bytes)

    return _runner


def test_ocr_success_reingests_the_file_and_reopens_extraction(monkeypatch, tmp_path):
    ocr, db_path, filings_dir = _fresh(monkeypatch, tmp_path)
    _seed_scanned_filing(db_path, filings_dir)

    counts = ocr.ocr_pending(
        project_root=tmp_path,
        runner=_make_runner(b"ocr'd bytes with a text layer"),
    )
    assert counts.considered == 1
    assert counts.ok == 1
    assert counts.pending_after == 0

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT is_scanned, extracted_utc, ocr_attempted_utc, size_bytes, sha256 "
            "FROM filings"
        ).fetchone()
    assert row["is_scanned"] == 0
    assert row["extracted_utc"] is None       # re-queued for the extractor
    assert row["ocr_attempted_utc"] is not None
    assert row["size_bytes"] == len(b"ocr'd bytes with a text layer")
    assert row["sha256"] != "original-sha"


def test_ocr_failure_stamps_attempt_but_leaves_is_scanned(monkeypatch, tmp_path):
    ocr, db_path, filings_dir = _fresh(monkeypatch, tmp_path)
    _seed_scanned_filing(db_path, filings_dir)

    def _fail(inp: Path, out: Path) -> None:
        raise subprocess.CalledProcessError(2, ["ocrmypdf"])

    counts = ocr.ocr_pending(project_root=tmp_path, runner=_fail)
    assert counts.failed == 1
    assert counts.ok == 0
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT is_scanned, extracted_utc, ocr_attempted_utc FROM filings"
        ).fetchone()
    assert row["is_scanned"] == 1
    assert row["extracted_utc"] is not None    # untouched from the 4b pass
    assert row["ocr_attempted_utc"] is not None
    # A second pass must be a no-op — the row's attempt stamp filters it out.
    counts2 = ocr.ocr_pending(project_root=tmp_path, runner=_fail)
    assert counts2.considered == 0


def test_ocr_timeout_is_treated_as_a_failure(monkeypatch, tmp_path):
    ocr, db_path, filings_dir = _fresh(monkeypatch, tmp_path)
    _seed_scanned_filing(db_path, filings_dir)

    def _slow(inp: Path, out: Path) -> None:
        raise subprocess.TimeoutExpired(cmd=["ocrmypdf"], timeout=1)

    counts = ocr.ocr_pending(project_root=tmp_path, runner=_slow)
    assert counts.failed == 1
    with connect(db_path) as conn:
        row = conn.execute("SELECT ocr_attempted_utc FROM filings").fetchone()
    assert row["ocr_attempted_utc"] is not None


def test_ocr_already_ocr_return_code_flips_the_flag(monkeypatch, tmp_path):
    """Some PDFs get flagged is_scanned=1 by pypdf but actually have text.
    ocrmypdf returns code 6 in that case; the pass should still un-flag
    is_scanned so the extractor picks the file up next run."""
    ocr, db_path, filings_dir = _fresh(monkeypatch, tmp_path)
    _seed_scanned_filing(db_path, filings_dir)

    def _prior(inp: Path, out: Path) -> None:
        raise subprocess.CalledProcessError(6, ["ocrmypdf"])

    counts = ocr.ocr_pending(project_root=tmp_path, runner=_prior)
    assert counts.already_had_text == 1
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT is_scanned, extracted_utc, ocr_attempted_utc FROM filings"
        ).fetchone()
    assert row["is_scanned"] == 0
    assert row["extracted_utc"] is None
    assert row["ocr_attempted_utc"] is not None


def test_missing_binary_aborts_pass_without_stamping_untouched_rows(monkeypatch, tmp_path):
    ocr, db_path, filings_dir = _fresh(monkeypatch, tmp_path)
    _seed_scanned_filing(db_path, filings_dir, fname="a.pdf")
    _seed_scanned_filing(db_path, filings_dir, fname="b.pdf", migrate=False)

    def _unavailable(inp: Path, out: Path) -> None:
        raise ocr.OcrUnavailable("ocrmypdf missing")

    counts = ocr.ocr_pending(project_root=tmp_path, runner=_unavailable)
    assert counts.unavailable == 1
    assert counts.ok == 0
    # First row was considered — we don't stamp it either, so the operator
    # can re-run once the binary is installed and pick up right where we
    # left off.
    with connect(db_path) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM filings "
            "WHERE is_scanned = 1 AND ocr_attempted_utc IS NULL"
        ).fetchone()[0]
    assert pending == 2


def test_missing_file_is_stamped_but_not_replaced(monkeypatch, tmp_path):
    """The file might have been deleted from disk between the 4b probe and
    now — mark the row so we don't select it again."""
    ocr, db_path, filings_dir = _fresh(monkeypatch, tmp_path)
    path = _seed_scanned_filing(db_path, filings_dir)
    path.unlink()

    def _boom(inp: Path, out: Path) -> None:  # pragma: no cover - guard
        raise AssertionError("runner shouldn't be called for missing files")

    counts = ocr.ocr_pending(project_root=tmp_path, runner=_boom)
    assert counts.missing_file == 1
    with connect(db_path) as conn:
        row = conn.execute("SELECT ocr_attempted_utc FROM filings").fetchone()
    assert row["ocr_attempted_utc"] is not None


def test_page_cap_filters_out_pathological_scans(monkeypatch, tmp_path):
    ocr, db_path, filings_dir = _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("OCR_MAX_PAGES", "50")
    _seed_scanned_filing(db_path, filings_dir, fname="ok.pdf", page_count=42)
    _seed_scanned_filing(db_path, filings_dir, fname="huge.pdf", page_count=300, migrate=False)

    import brvm.config as cfg
    importlib.reload(cfg)
    importlib.reload(ocr)

    calls: list[str] = []

    def _runner(inp: Path, out: Path) -> None:
        calls.append(inp.name)
        out.write_bytes(b"ok")

    counts = ocr.ocr_pending(project_root=tmp_path, runner=_runner)
    assert counts.considered == 1
    assert calls == ["ok.pdf"]


def test_repo_helpers_pending_ocr_query(tmp_db_path):
    """`list_pending_ocr` filters on is_scanned=1 AND ocr_attempted IS NULL."""
    with connect(tmp_db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [Security(ticker="X", name="X", kind="equity")])
        filings_repo.upsert_filings(conn, [Filing(
            ticker="X", issuer_name="X", doc_type="rapport_annuel",
            source="s", source_url="u1", url_hash="h1",
            file_path="p1", size_bytes=1, sha256="a",
        )])
        conn.execute("UPDATE filings SET is_scanned = 1")
        conn.commit()

        assert len(filings_repo.list_pending_ocr(conn)) == 1
        assert filings_repo.count_pending_ocr(conn) == 1

        filings_repo.apply_ocr_failure(conn, 1)
        assert filings_repo.list_pending_ocr(conn) == []
        assert filings_repo.count_pending_ocr(conn) == 0


@pytest.mark.parametrize(
    ("returncode", "expected_reason"),
    [(2, "nonzero_exit:2"), (6, "already_ocr")],
)
def test_ocr_file_returns_reason_for_common_failures(tmp_path, returncode, expected_reason):
    import brvm.services.ocr as ocr_mod

    src = tmp_path / "src.pdf"
    src.write_bytes(b"junk")

    def _r(inp: Path, out: Path) -> None:
        raise subprocess.CalledProcessError(returncode, ["ocrmypdf"])

    outcome = ocr_mod.ocr_file(src, runner=_r)
    assert not outcome.ok
    assert outcome.reason == expected_reason
