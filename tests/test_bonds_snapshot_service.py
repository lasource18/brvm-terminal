"""Service-level smoke test for bond ingestion.

Fetches are mocked — the real network calls belong to the parser
fixtures. This test guards the wiring: `snapshot_bonds_once` writes to
`securities`, `daily_bars`, and `bond_snapshots`, and re-running it
upserts idempotently.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brvm.db import connect
from brvm.models import BondSnapshot, DailyBar, Security


def _fake_bonds() -> tuple[list[Security], list[DailyBar], list[BondSnapshot]]:
    session = date(2026, 8, 27)
    return (
        [
            Security(
                ticker="EOM.O10",
                name="ETAT DU MALI 6,20% 2022-2029",
                kind="bond",
                country="ML",
                sector="Obligations d'Etat",
                source_url="https://www.brvm.org/fr/cours-obligations/20",
                coupon_rate=6.20,
                maturity_year=2029,
                issue_date=date(2023, 2, 15),
                issuer_name="ETAT DU MALI",
            ),
            Security(
                ticker="BIDC.O4",
                name="BIDC-EBID 6,10% 2017-2027",
                kind="bond",
                sector="Obligations d'Etat",
                source_url="https://www.brvm.org/fr/cours-obligations/20",
                coupon_rate=6.10,
                maturity_year=2027,
                issue_date=date(2017, 11, 30),
                issuer_name="BIDC-EBID",
            ),
        ],
        [
            DailyBar(ticker="EOM.O10", session_date=session, close=10000.0, source="brvm_org"),
            DailyBar(ticker="BIDC.O4", session_date=session, close=1250.0, source="brvm_org"),
        ],
        [
            BondSnapshot(
                ticker="EOM.O10", session_date=session, accrued_coupon=441.64,
                last_coupon_date=date(2025, 12, 9), last_coupon_amount=620.0,
                source="brvm_org",
            ),
            BondSnapshot(
                ticker="BIDC.O4", session_date=session, accrued_coupon=14.83,
                last_coupon_date=date(2026, 6, 16), last_coupon_amount=76.25,
                source="brvm_org",
            ),
        ],
    )


def _apply_migrations(db_path: Path) -> None:
    from brvm.db import ensure_migrations_table

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with connect(db_path) as conn:
        ensure_migrations_table(conn)
        for f in sorted(migrations.glob("*.sql")):
            conn.executescript(f.read_text(encoding="utf-8"))
        conn.commit()


def test_snapshot_bonds_once_writes_rows(monkeypatch, tmp_path):
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    from brvm.config import reset_settings_cache

    reset_settings_cache()
    _apply_migrations(db_path)

    monkeypatch.setattr(
        "brvm.services.quotes.brvm_org_bonds.fetch_all_bonds",
        lambda client=None, today=None: _fake_bonds(),
    )

    from brvm.services.quotes import snapshot_bonds_once

    counts = snapshot_bonds_once()
    assert counts == {"securities": 2, "bars": 2, "snapshots": 2}

    with connect(db_path) as conn:
        secs = conn.execute(
            """
            SELECT ticker, kind, country, sector, coupon_rate, maturity_year,
                   issue_date, issuer_name
            FROM securities ORDER BY ticker
            """
        ).fetchall()
        bars = conn.execute(
            "SELECT ticker, close, source FROM daily_bars ORDER BY ticker"
        ).fetchall()
        snaps = conn.execute(
            """
            SELECT ticker, accrued_coupon, last_coupon_date, last_coupon_amount
            FROM bond_snapshots ORDER BY ticker
            """
        ).fetchall()

    assert [r["ticker"] for r in secs] == ["BIDC.O4", "EOM.O10"]
    assert {r["kind"] for r in secs} == {"bond"}
    mali_row = dict(secs[1])
    assert mali_row["country"] == "ML"
    assert mali_row["coupon_rate"] == 6.20
    assert mali_row["maturity_year"] == 2029
    assert mali_row["issue_date"] == "2023-02-15"
    assert mali_row["issuer_name"] == "ETAT DU MALI"
    assert [r["source"] for r in bars] == ["brvm_org", "brvm_org"]
    assert dict(bars[1])["close"] == 10000.0
    mali_snap = dict(snaps[1])
    assert mali_snap["accrued_coupon"] == 441.64
    assert mali_snap["last_coupon_date"] == "2025-12-09"
    assert mali_snap["last_coupon_amount"] == 620.0

    # Idempotency: same fetch shouldn't create duplicates.
    counts2 = snapshot_bonds_once()
    assert counts2 == {"securities": 2, "bars": 2, "snapshots": 2}
    with connect(db_path) as conn:
        (n_secs,) = conn.execute("SELECT COUNT(*) FROM securities").fetchone()
        (n_bars,) = conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()
        (n_snaps,) = conn.execute("SELECT COUNT(*) FROM bond_snapshots").fetchone()
    assert n_secs == 2
    assert n_bars == 2
    assert n_snaps == 2
