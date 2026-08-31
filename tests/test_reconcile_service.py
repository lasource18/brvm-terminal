"""Tests for `services.reconcile.check_boc_close`."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from kodji.db import connect
from kodji.models import DailyBar, Security
from kodji.services import reconcile
from kodji.store import quotes as quotes_repo
from kodji.store import securities as sec_repo


def _apply_migrations(db_path: Path) -> None:
    from kodji.db import ensure_migrations_table

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
    db_path = tmp_path / "kodji.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    from kodji.config import reset_settings_cache
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
    db_path = tmp_path / "kodji.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    from kodji.config import reset_settings_cache
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
    db_path = tmp_path / "kodji.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    from kodji.config import reset_settings_cache
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


def test_session_date_defaults_to_boc_pdf_date(
    monkeypatch, tmp_path, fixtures_dir
):
    """F-04: production callers pass neither `session_date` nor
    `pdf_bytes`. The scheduler-invoked path must fetch the PDF and
    take the session date from the PDF's filename, NOT from
    `MAX(daily_bars.session_date)` — which for equity tickers is
    typically the most recent weekly-backfill row, days apart from
    the BOC's date."""
    db_path = tmp_path / "kodji.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    from kodji.config import reset_settings_cache
    reset_settings_cache()
    _apply_migrations(db_path)

    # Seed daily_bars for a *different* date to prove _latest_session()
    # isn't the source of the returned session_date.
    _seed(db_path, date(2026, 8, 11))

    from kodji.sources import brvm_org as brvm_org_module
    pdf = (fixtures_dir / "brvm_org" / "boc_eng_20260818_2.pdf").read_bytes()

    def _fake_fetch(lang: str = "eng") -> brvm_org_module.BocFetch:
        return brvm_org_module.BocFetch(
            pdf_bytes=pdf, session_date=date(2026, 8, 18),
        )

    monkeypatch.setattr(brvm_org_module, "fetch_boc", _fake_fetch)
    report = reconcile.check_boc_close(tolerance_pct=0.01)
    # The BOC's filename-encoded date wins over `MAX(daily_bars)`.
    assert report.session_date == date(2026, 8, 18)


def test_session_date_falls_back_to_latest_when_boc_pdf_lacks_date(
    monkeypatch, tmp_path, fixtures_dir
):
    """Malformed BOC filename (no YYYYMMDD block) falls back to
    _latest_session() so the reconciliation still runs against
    something rather than silently returning session_date=None."""
    db_path = tmp_path / "kodji.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    from kodji.config import reset_settings_cache
    reset_settings_cache()
    _apply_migrations(db_path)
    _seed(db_path, date(2026, 8, 11))

    from kodji.sources import brvm_org as brvm_org_module
    pdf = (fixtures_dir / "brvm_org" / "boc_eng_20260818_2.pdf").read_bytes()

    def _fake_fetch(lang: str = "eng") -> brvm_org_module.BocFetch:
        return brvm_org_module.BocFetch(pdf_bytes=pdf, session_date=None)

    monkeypatch.setattr(brvm_org_module, "fetch_boc", _fake_fetch)
    report = reconcile.check_boc_close(tolerance_pct=0.01)
    assert report.session_date == date(2026, 8, 11)


def test_report_flags_has_drift():
    from kodji.services.reconcile import CloseDrift, ReconcileReport
    empty = ReconcileReport(session_date=None, boc_rows=0, matched=0, drift=[])
    assert not empty.has_drift
    populated = ReconcileReport(
        session_date=date(2026, 8, 18), boc_rows=1, matched=0,
        drift=[CloseDrift(ticker="X", boc_close=100.0,
                          local_close=99.0, delta_pct=-1.0)],
    )
    assert populated.has_drift
