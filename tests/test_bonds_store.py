"""Tests for the bond_snapshots repo + list_by_issuer read helper."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brvm.db import connect
from brvm.models import BondSnapshot, Security
from brvm.store import bonds as bonds_repo
from brvm.store import securities as sec_repo


def _apply_migrations(db_path: Path) -> None:
    from brvm.db import ensure_migrations_table

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with connect(db_path) as conn:
        ensure_migrations_table(conn)
        for f in sorted(migrations.glob("*.sql")):
            conn.executescript(f.read_text(encoding="utf-8"))
        conn.commit()


def _mali_bonds() -> list[Security]:
    common = dict(kind="bond", country="ML", sector="Obligations d'Etat",
                  issuer_name="ETAT DU MALI")
    return [
        Security(ticker="EOM.O10", name="ETAT DU MALI 6,20% 2022-2029",
                 coupon_rate=6.20, maturity_year=2029,
                 issue_date=date(2023, 2, 15), **common),
        Security(ticker="EOM.O13", name="ETAT DU MALI 6,50% 2024-2034",
                 coupon_rate=6.50, maturity_year=2034,
                 issue_date=date(2024, 6, 27), **common),
        Security(ticker="EOM.O11", name="ETAT DU MALI 6,40% 2023-2030",
                 coupon_rate=6.40, maturity_year=2030,
                 issue_date=date(2023, 5, 17), **common),
    ]


def test_list_by_issuer_sorts_by_maturity(tmp_path):
    db_path = tmp_path / "brvm.sqlite"
    _apply_migrations(db_path)
    with connect(db_path) as conn:
        sec_repo.upsert(conn, _mali_bonds())
        rows = bonds_repo.list_by_issuer(conn, "ETAT DU MALI")
    assert [r["ticker"] for r in rows] == ["EOM.O10", "EOM.O11", "EOM.O13"]
    assert [r["maturity_year"] for r in rows] == [2029, 2030, 2034]


def test_list_by_issuer_excludes_self(tmp_path):
    db_path = tmp_path / "brvm.sqlite"
    _apply_migrations(db_path)
    with connect(db_path) as conn:
        sec_repo.upsert(conn, _mali_bonds())
        rows = bonds_repo.list_by_issuer(conn, "ETAT DU MALI", exclude_ticker="EOM.O10")
    assert [r["ticker"] for r in rows] == ["EOM.O11", "EOM.O13"]


def test_upsert_snapshots_and_latest(tmp_path):
    db_path = tmp_path / "brvm.sqlite"
    _apply_migrations(db_path)
    with connect(db_path) as conn:
        sec_repo.upsert(conn, _mali_bonds()[:1])
        bonds_repo.upsert_snapshots(conn, [
            BondSnapshot(
                ticker="EOM.O10", session_date=date(2026, 8, 20),
                accrued_coupon=430.0,
                last_coupon_date=date(2025, 12, 9), last_coupon_amount=620.0,
                source="brvm_org",
            ),
            BondSnapshot(
                ticker="EOM.O10", session_date=date(2026, 8, 27),
                accrued_coupon=441.64,
                last_coupon_date=date(2025, 12, 9), last_coupon_amount=620.0,
                source="brvm_org",
            ),
        ])
        latest = bonds_repo.latest_snapshot(conn, "EOM.O10")

    assert latest is not None
    assert latest.session_date == date(2026, 8, 27)
    assert latest.accrued_coupon == 441.64
    assert latest.last_coupon_date == date(2025, 12, 9)


def test_upsert_snapshots_upserts_same_session(tmp_path):
    db_path = tmp_path / "brvm.sqlite"
    _apply_migrations(db_path)
    with connect(db_path) as conn:
        sec_repo.upsert(conn, _mali_bonds()[:1])
        session = date(2026, 8, 27)
        bonds_repo.upsert_snapshots(conn, [
            BondSnapshot(ticker="EOM.O10", session_date=session,
                         accrued_coupon=440.0, source="brvm_org"),
        ])
        # Re-upsert with a corrected accrued value — should overwrite, not
        # duplicate.
        bonds_repo.upsert_snapshots(conn, [
            BondSnapshot(ticker="EOM.O10", session_date=session,
                         accrued_coupon=441.64, source="brvm_org"),
        ])
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM bond_snapshots WHERE ticker='EOM.O10'"
        ).fetchone()
        (v,) = conn.execute(
            "SELECT accrued_coupon FROM bond_snapshots WHERE ticker='EOM.O10'"
        ).fetchone()
    assert n == 1
    assert v == 441.64
