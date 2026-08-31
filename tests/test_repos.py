from datetime import date
from pathlib import Path

from kodji.db import connect, ensure_migrations_table
from kodji.models import DailyBar, IndexLevel, Quote, Security
from kodji.store import quotes as quotes_repo
from kodji.store import securities as sec_repo


def _init(tmp_db_path: Path) -> None:
    root = Path(__file__).resolve().parents[1] / "migrations"
    with connect(tmp_db_path) as conn:
        ensure_migrations_table(conn)
        for f in sorted(root.glob("*.sql")):
            conn.executescript(f.read_text())
        conn.commit()


def test_securities_upsert(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        n = sec_repo.upsert(
            conn,
            [
                Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
                Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
            ],
        )
        assert n == 2
        assert sec_repo.count(conn) == 2
        assert sec_repo.count(conn, "equity") == 1

        # Re-upsert should NOT duplicate and should refresh name.
        sec_repo.upsert(
            conn,
            [Security(ticker="SNTS", name="SONATEL SA", kind="equity", country="SN")],
        )
        assert sec_repo.count(conn) == 2
        row = conn.execute("SELECT name FROM securities WHERE ticker='SNTS'").fetchone()
        assert row["name"] == "SONATEL SA"


def test_snapshot_and_top_by_turnover(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        sec_repo.upsert(
            conn,
            [
                Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
                Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
            ],
        )
        n = quotes_repo.insert_snapshots(
            conn,
            [
                Quote(ticker="SNTS", source="sikafinance", last=32500, volume=3006, turnover=97_695_000),
                Quote(ticker="ORAC", source="sikafinance", last=19000, volume=1000, turnover=19_000_000),
            ],
        )
        assert n == 2
        rows = quotes_repo.latest_snapshot_by_ticker(conn)
        assert rows[0]["ticker"] == "SNTS"  # higher turnover ranks first
        assert rows[1]["ticker"] == "ORAC"


def test_daily_bars_and_index_levels_are_idempotent(tmp_db_path: Path):
    _init(tmp_db_path)
    with connect(tmp_db_path) as conn:
        sec_repo.upsert(
            conn,
            [
                Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
                Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
            ],
        )
        bar = DailyBar(
            ticker="SNTS",
            session_date=date(2026, 8, 18),
            close=32500,
            open=31990,
            high=32500,
            low=31990,
            volume=3006,
            turnover=97_695_000,
            source="sikafinance",
        )
        quotes_repo.upsert_daily_bars(conn, [bar, bar])  # dup on purpose
        assert conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0] == 1

        lvl = IndexLevel(
            ticker="BRVMC",
            session_date=date(2026, 8, 18),
            level=507.13,
            change_pct=1.16,
            source="sikafinance",
        )
        quotes_repo.upsert_index_levels(conn, [lvl, lvl])
        assert conn.execute("SELECT COUNT(*) FROM index_levels").fetchone()[0] == 1
