"""Service-level smoke test for bond ingestion.

Fetches are mocked — the real network calls belong to the parser
fixtures. This test guards the wiring: `snapshot_bonds_once` writes to
`securities` and `daily_bars`, and re-running it upserts idempotently.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brvm.db import connect
from brvm.models import DailyBar, Security


def _fake_bonds() -> tuple[list[Security], list[DailyBar]]:
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
            ),
            Security(
                ticker="BIDC.O4",
                name="BIDC-EBID 6,10% 2017-2027",
                kind="bond",
                sector="Obligations d'Etat",
                source_url="https://www.brvm.org/fr/cours-obligations/20",
            ),
        ],
        [
            DailyBar(ticker="EOM.O10", session_date=session, close=10000.0, source="brvm_org"),
            DailyBar(ticker="BIDC.O4", session_date=session, close=1250.0, source="brvm_org"),
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
    assert counts == {"securities": 2, "bars": 2}

    with connect(db_path) as conn:
        secs = conn.execute(
            "SELECT ticker, kind, country, sector FROM securities ORDER BY ticker"
        ).fetchall()
        bars = conn.execute(
            "SELECT ticker, close, source FROM daily_bars ORDER BY ticker"
        ).fetchall()

    assert [r["ticker"] for r in secs] == ["BIDC.O4", "EOM.O10"]
    assert {r["kind"] for r in secs} == {"bond"}
    assert dict(secs[1])["country"] == "ML"
    assert [r["source"] for r in bars] == ["brvm_org", "brvm_org"]
    assert dict(bars[1])["close"] == 10000.0

    # Idempotency: same fetch shouldn't create duplicate securities and the
    # bar row should be an UPDATE (still one row per ticker+session_date).
    counts2 = snapshot_bonds_once()
    assert counts2 == {"securities": 2, "bars": 2}
    with connect(db_path) as conn:
        (n_secs,) = conn.execute("SELECT COUNT(*) FROM securities").fetchone()
        (n_bars,) = conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()
    assert n_secs == 2
    assert n_bars == 2
