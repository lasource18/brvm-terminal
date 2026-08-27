from pathlib import Path

from brvm.db import connect, ensure_migrations_table
from brvm.models import Security
from brvm.store import securities as sec_repo
from brvm.store import watchlists as wl_repo


def _init(tmp_db_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    with connect(tmp_db_path) as conn:
        ensure_migrations_table(conn)
        for f in sorted((root / "migrations").glob("*.sql")):
            conn.executescript(f.read_text())
        conn.commit()


class TestSlugify:
    def test_basic(self):
        assert wl_repo.slugify("Core Four") == "core-four"

    def test_strips_punct(self):
        assert wl_repo.slugify("  Banks & Telecom!!  ") == "banks-telecom"

    def test_empty_input_gives_fallback(self):
        assert wl_repo.slugify("") == "list"


class TestSeedAndCrud:
    def test_default_seeded(self, tmp_db_path):
        _init(tmp_db_path)
        with connect(tmp_db_path) as conn:
            rows = wl_repo.list_all(conn)
        slugs = [r["slug"] for r in rows]
        assert "default" in slugs

    def test_create_add_remove_cascade(self, tmp_db_path):
        _init(tmp_db_path)
        with connect(tmp_db_path) as conn:
            sec_repo.upsert(
                conn,
                [
                    Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
                    Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
                ],
            )
            wl_id = wl_repo.create(conn, "Core Four")
            assert wl_repo.add_item(conn, wl_id, "SNTS") is True
            assert wl_repo.add_item(conn, wl_id, "SNTS") is False  # dedupe
            assert wl_repo.add_item(conn, wl_id, "ORAC") is True

            items = wl_repo.items(conn, wl_id)
            assert [i["ticker"] for i in items] == ["SNTS", "ORAC"]

            assert wl_repo.remove_item(conn, wl_id, "SNTS") == 1
            assert [i["ticker"] for i in wl_repo.items(conn, wl_id)] == ["ORAC"]

            assert wl_repo.delete(conn, "core-four") == 1
            # cascade: items are gone too
            n = conn.execute(
                "SELECT COUNT(*) FROM watchlist_items WHERE watchlist_id = ?",
                (wl_id,),
            ).fetchone()[0]
            assert n == 0

    def test_unique_slug(self, tmp_db_path):
        import sqlite3

        _init(tmp_db_path)
        with connect(tmp_db_path) as conn:
            wl_repo.create(conn, "Alpha")
            try:
                wl_repo.create(conn, "Alpha")
            except sqlite3.IntegrityError:
                return
        raise AssertionError("expected IntegrityError on duplicate slug")
