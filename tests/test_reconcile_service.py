"""Tests for `services.reconcile.check_boc_close`."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brvm.db import connect
from brvm.models import DailyBar, Security
from brvm.services import reconcile
from brvm.store import quotes as quotes_repo
from brvm.store import securities as sec_repo


def _apply_migrations(db_path: Path) -> None:
    from brvm.db import ensure_migrations_table

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with connect(db_path) as conn:
        ensure_migrations_table(conn)
        for f in sorted(migrations.glob("*.sql")):
            conn.executescript(f.read_text(encoding="utf-8"))
        conn.commit()


def _seed(db_path: Path, session: date) -> None:
    with connect(db_path) as conn:
        sec_repo.upsert(conn, [
            Security(ticker="CIEC", name="CIE CI", kind="equity", country="CI"),
            Security(ticker="UNLC", name="UNILEVER CI", kind="equity", country="CI"),
            Security(ticker="STBC", name="SITAB CI", kind="equity", country="CI"),
        ])
        quotes_repo.upsert_daily_bars(conn, [
            # CIEC matches BOC (6360.0)
            DailyBar(ticker="CIEC", session_date=session,
                     close=6360.0, source="test"),
            # UNLC drifts (BOC says 54000.0; local reports 53900.0)
            DailyBar(ticker="UNLC", session_date=session,
                     close=53900.0, source="test"),
            # STBC missing from BOC's parseable rows in general isn't
            # what this test needs — instead let STBC exist locally
            # so `_local_closes_for_session` returns it.
            DailyBar(ticker="STBC", session_date=session,
                     close=23000.0, source="test"),
        ])


def test_matches_within_tolerance(monkeypatch, tmp_path, fixtures_dir):
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    from brvm.config import reset_settings_cache
    reset_settings_cache()
    _apply_migrations(db_path)
    session = date(2026, 8, 18)
    _seed(db_path, session)

    pdf = (fixtures_dir / "brvm_org" / "boc_eng_20260818_2.pdf").read_bytes()
    report = reconcile.check_boc_close(
        session_date=session, pdf_bytes=pdf, tolerance_pct=0.01,
    )
    assert report.boc_rows >= 25
    assert report.session_date == session

    drift_tickers = {d.ticker for d in report.drift}
    # CIEC matches at 6,360 — must not appear in drift.
    assert "CIEC" not in drift_tickers
    # UNLC's local 53,900 is 0.185% below the BOC's 54,000 — flagged.
    assert "UNLC" in drift_tickers
    unlc = next(d for d in report.drift if d.ticker == "UNLC")
    assert unlc.boc_close == 54_000.0
    assert unlc.local_close == 53_900.0
    assert unlc.delta_pct is not None
    assert abs(unlc.delta_pct + 0.185) < 0.01


def test_missing_local_row_reported_as_no_delta(monkeypatch, tmp_path, fixtures_dir):
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    from brvm.config import reset_settings_cache
    reset_settings_cache()
    _apply_migrations(db_path)
    session = date(2026, 8, 18)
    # Seed *no* local rows so every BOC ticker surfaces as
    # `local_close=None, delta_pct=None`.
    with connect(db_path) as conn:
        # Need at least one row so `_latest_session()` finds it.
        sec_repo.upsert(conn, [Security(ticker="X", name="X", kind="equity")])
        quotes_repo.upsert_daily_bars(conn, [
            DailyBar(ticker="X", session_date=session, close=1.0, source="test"),
        ])

    pdf = (fixtures_dir / "brvm_org" / "boc_eng_20260818_2.pdf").read_bytes()
    report = reconcile.check_boc_close(
        session_date=session, pdf_bytes=pdf, tolerance_pct=0.01,
    )
    # Every parsed BOC ticker should be in drift because none of them
    # are in daily_bars.
    assert len(report.drift) == report.boc_rows
    assert all(d.local_close is None for d in report.drift)
    assert all(d.delta_pct is None for d in report.drift)
    assert report.matched == 0


def test_empty_pdf_returns_empty_report(monkeypatch, tmp_path):
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    from brvm.config import reset_settings_cache
    reset_settings_cache()
    _apply_migrations(db_path)
    # No pypdf-parseable content and no local bars.
    report = reconcile.check_boc_close(
        session_date=date(2026, 8, 18),
        pdf_bytes=b"",
        tolerance_pct=0.01,
    )
    assert report.boc_rows == 0
    assert report.drift == []


def test_report_flags_has_drift():
    from brvm.services.reconcile import CloseDrift, ReconcileReport
    empty = ReconcileReport(session_date=None, boc_rows=0, matched=0, drift=[])
    assert not empty.has_drift
    populated = ReconcileReport(
        session_date=date(2026, 8, 18), boc_rows=1, matched=0,
        drift=[CloseDrift(ticker="X", boc_close=100.0,
                          local_close=99.0, delta_pct=-1.0)],
    )
    assert populated.has_drift
