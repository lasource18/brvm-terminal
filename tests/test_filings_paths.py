"""F-23: `filings.file_path` must persist project-relative so the
corpus stays portable across a Mac → VPS deploy. Pin the boundary
between filesystem I/O (absolute) and DB storage (relative)."""

from __future__ import annotations

from pathlib import Path

from brvm.config import reset_settings_cache
from brvm.db import connect
from brvm.services import filings as svc

from .conftest import apply_migrations


def test_relativize_strips_project_root():
    root = svc._project_root()
    dest = root / "data" / "filings" / "SNTS" / "annual.pdf"
    assert svc._relativize(dest) == "data/filings/SNTS/annual.pdf"


def test_relativize_leaves_paths_outside_project_absolute(tmp_path):
    """A corpus rooted somewhere else (an operator override to
    `/mnt/big` or similar) has nothing safe to strip. `_resolve_path`
    on the reader side already handles both forms, so preserving the
    absolute form is fine."""
    outsider = tmp_path.resolve() / "extern" / "corpus" / "SNTS" / "x.pdf"
    outsider.parent.mkdir(parents=True, exist_ok=True)
    outsider.touch()
    got = svc._relativize(outsider)
    assert Path(got).is_absolute()
    assert got == str(outsider)


def test_resolve_path_round_trip_from_relativize():
    """A path written by `_relativize` reads back to the same
    absolute location via `_resolve_path` (the helper used by the
    ocr / fundamentals extract paths)."""
    from brvm.services.fundamentals import _resolve_path
    root = svc._project_root()
    dest = root / "data" / "filings" / "SNTS" / "annual.pdf"
    relative = svc._relativize(dest)
    resolved = _resolve_path(root, relative)
    assert resolved == dest


def test_rewrite_filings_paths_script_migrates_absolute_rows(
    monkeypatch, tmp_path
):
    """The one-shot `scripts/rewrite_filings_paths.py` rewrites
    absolute `filings.file_path` rows to their project-relative
    form. Idempotent: rows already relative are left alone, and
    absolute paths outside the project stay absolute."""
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from brvm.models import Filing, Security
    from brvm.store import filings as filings_repo
    from brvm.store import securities as sec_repo

    project_root = svc._project_root()

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
        ])
        # One row with an in-project absolute path (should be rewritten).
        # One row already relative (should be left alone).
        # One row with an absolute path outside the project (should stay).
        filings_repo.upsert_filings(conn, [
            Filing(
                ticker="SNTS", issuer_name="SONATEL",
                doc_type="rapport_annuel", period_kind="annual", period_year=2024,
                source="brvm_org", source_url="https://x/a.pdf", url_hash="hA",
                file_path=str(project_root / "data/filings/SNTS/a.pdf"),
                size_bytes=1, sha256="a", page_count=1,
            ),
            Filing(
                ticker="SNTS", issuer_name="SONATEL",
                doc_type="rapport_annuel", period_kind="annual", period_year=2023,
                source="brvm_org", source_url="https://x/b.pdf", url_hash="hB",
                file_path="data/filings/SNTS/b.pdf",  # already relative
                size_bytes=1, sha256="b", page_count=1,
            ),
            Filing(
                ticker="SNTS", issuer_name="SONATEL",
                doc_type="rapport_annuel", period_kind="annual", period_year=2022,
                source="brvm_org", source_url="https://x/c.pdf", url_hash="hC",
                file_path="/mnt/external/SNTS/c.pdf",  # outside project
                size_bytes=1, sha256="c", page_count=1,
            ),
        ])

    import sys
    sys.path.insert(0, str(project_root / "scripts"))
    import rewrite_filings_paths as rw
    rw.run(apply=True)

    with connect(db_path) as conn:
        paths = {
            r["url_hash"]: r["file_path"]
            for r in conn.execute(
                "SELECT url_hash, file_path FROM filings"
            ).fetchall()
        }
    assert paths["hA"] == "data/filings/SNTS/a.pdf"
    assert paths["hB"] == "data/filings/SNTS/b.pdf"
    assert paths["hC"] == "/mnt/external/SNTS/c.pdf"
