"""Tests for `services.bonds` — schedule builder, YTM solver, duration,
convexity, and the composed bond view."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brvm.db import connect
from brvm.models import BondSnapshot, DailyBar, Security
from brvm.services import bonds as bonds_svc
from brvm.store import bonds as bonds_repo
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


TODAY = date(2026, 8, 27)


# --------- schedule builder ------------------------------------------------


class TestBuildSchedule:
    def test_returns_none_without_coupon_or_maturity(self):
        s = bonds_svc.build_schedule(
            coupon_rate=None, maturity_year=2029,
            last_coupon_date=date(2025, 12, 9), issue_date=None, today=TODAY,
        )
        assert s is None

    def test_returns_none_without_anchor(self):
        s = bonds_svc.build_schedule(
            coupon_rate=6.20, maturity_year=2029,
            last_coupon_date=None, issue_date=None, today=TODAY,
        )
        assert s is None

    def test_prefers_last_coupon_over_issue_date(self):
        # Anchor should be last_coupon_date (2025-12-09), not the
        # issue-date anniversary (2023-02-15), because the exchange
        # actually paid on 2025-12-09.
        s = bonds_svc.build_schedule(
            coupon_rate=6.20, maturity_year=2029,
            last_coupon_date=date(2025, 12, 9),
            issue_date=date(2023, 2, 15),
            today=TODAY,
        )
        assert s is not None
        assert s.next_coupon_date == date(2026, 12, 9)

    def test_falls_back_to_issue_date_when_no_last_coupon(self):
        s = bonds_svc.build_schedule(
            coupon_rate=6.20, maturity_year=2029,
            last_coupon_date=None,
            issue_date=date(2023, 2, 15),
            today=TODAY,
        )
        assert s is not None
        # 2023-02-15 → next anniversary after 2026-08-27 is 2027-02-15
        assert s.next_coupon_date == date(2027, 2, 15)

    def test_terminal_row_carries_principal(self):
        s = bonds_svc.build_schedule(
            coupon_rate=6.20, maturity_year=2029,
            last_coupon_date=date(2025, 12, 9), issue_date=None, today=TODAY,
        )
        assert s is not None
        assert s.rows[-1].principal == bonds_svc.DEFAULT_NOMINAL_XOF
        assert s.rows[-1].is_terminal
        # Non-terminal rows carry only the coupon.
        for r in s.rows[:-1]:
            assert r.principal == 0.0
            assert not r.is_terminal

    def test_annual_coupon_amount(self):
        # 6.20% x 10 000 = 620
        s = bonds_svc.build_schedule(
            coupon_rate=6.20, maturity_year=2029,
            last_coupon_date=date(2025, 12, 9), issue_date=None, today=TODAY,
        )
        assert s is not None
        assert s.annual_coupon == 620.0
        assert all(r.coupon == 620.0 for r in s.rows)

    def test_coupons_remaining_matches_row_count(self):
        s = bonds_svc.build_schedule(
            coupon_rate=6.20, maturity_year=2029,
            last_coupon_date=date(2025, 12, 9), issue_date=None, today=TODAY,
        )
        assert s is not None
        assert s.coupons_remaining == len(s.rows)


# --------- YTM solver ------------------------------------------------------


class TestSolveYtm:
    def _bullet(self, coupon_pct: float, years: int) -> list[bonds_svc.CashFlowRow]:
        rows: list[bonds_svc.CashFlowRow] = []
        nominal = 10000.0
        coupon = coupon_pct / 100 * nominal
        for i in range(1, years + 1):
            principal = nominal if i == years else 0.0
            rows.append(bonds_svc.CashFlowRow(
                payment_date=date(2026 + i, 1, 1),
                coupon=coupon, principal=principal,
                total=coupon + principal,
                year_fraction=float(i),
            ))
        return rows

    def test_at_par_ytm_equals_coupon(self):
        # A bullet at par has YTM = coupon rate (classic identity).
        rows = self._bullet(6.0, 5)
        y = bonds_svc.solve_ytm(rows, dirty_price=10_000.0)
        assert y is not None
        assert abs(y - 0.06) < 1e-4

    def test_below_par_ytm_above_coupon(self):
        # Trading at a discount → YTM > coupon.
        rows = self._bullet(6.0, 5)
        y = bonds_svc.solve_ytm(rows, dirty_price=9_500.0)
        assert y is not None
        assert y > 0.06

    def test_above_par_ytm_below_coupon(self):
        # Trading at a premium → YTM < coupon.
        rows = self._bullet(6.0, 5)
        y = bonds_svc.solve_ytm(rows, dirty_price=10_500.0)
        assert y is not None
        assert y < 0.06

    def test_zero_price_returns_none(self):
        rows = self._bullet(6.0, 5)
        assert bonds_svc.solve_ytm(rows, dirty_price=0.0) is None

    def test_empty_rows_returns_none(self):
        assert bonds_svc.solve_ytm([], dirty_price=10_000.0) is None


# --------- duration & convexity -------------------------------------------


class TestDurationConvexity:
    def _bullet(self, coupon_pct: float, years: int) -> list[bonds_svc.CashFlowRow]:
        rows: list[bonds_svc.CashFlowRow] = []
        nominal = 10000.0
        coupon = coupon_pct / 100 * nominal
        for i in range(1, years + 1):
            principal = nominal if i == years else 0.0
            rows.append(bonds_svc.CashFlowRow(
                payment_date=date(2026 + i, 1, 1),
                coupon=coupon, principal=principal,
                total=coupon + principal,
                year_fraction=float(i),
            ))
        return rows

    def test_modified_duration_less_than_maturity(self):
        # A 5y bullet at par has Macaulay < 5 (due to interim coupons).
        rows = self._bullet(6.0, 5)
        y = 0.06
        mac = bonds_svc.macaulay_duration(rows, y, 10_000.0)
        mod = bonds_svc.modified_duration(rows, y, 10_000.0)
        assert mac is not None and mod is not None
        assert 3.5 < mac < 5.0
        assert mod == mac / (1.0 + y)

    def test_convexity_positive(self):
        rows = self._bullet(6.0, 5)
        c = bonds_svc.convexity(rows, 0.06, 10_000.0)
        assert c is not None
        assert c > 0


# --------- current yield ---------------------------------------------------


class TestCurrentYield:
    def test_at_par(self):
        # 6.20% coupon x 10 000 / 10 000 = 6.20% current yield.
        assert bonds_svc.current_yield(6.20, 10_000.0) == 6.20

    def test_below_par_boosts_yield(self):
        # Same coupon at a lower price → higher current yield.
        y = bonds_svc.current_yield(6.20, 8_000.0)
        assert y is not None and y > 6.20

    def test_returns_none_when_missing(self):
        assert bonds_svc.current_yield(None, 10_000.0) is None
        assert bonds_svc.current_yield(6.20, None) is None
        assert bonds_svc.current_yield(6.20, 0.0) is None


# --------- get_bond_view composition + related bonds ----------------------


def _seed_mali(conn) -> None:
    common = dict(kind="bond", country="ML", sector="Obligations d'Etat",
                  issuer_name="ETAT DU MALI")
    sec_repo.upsert(conn, [
        Security(ticker="EOM.O10", name="ETAT DU MALI 6,20% 2022-2029",
                 coupon_rate=6.20, maturity_year=2029,
                 issue_date=date(2023, 2, 15), **common),
        Security(ticker="EOM.O11", name="ETAT DU MALI 6,40% 2023-2030",
                 coupon_rate=6.40, maturity_year=2030,
                 issue_date=date(2023, 5, 17), **common),
        Security(ticker="EOM.O2", name="ETAT DU MALI 6.50% 2017-2024",
                 coupon_rate=6.50, maturity_year=2024,
                 issue_date=date(2017, 8, 31), **common),
    ])
    quotes_repo.upsert_daily_bars(conn, [
        DailyBar(ticker="EOM.O10", session_date=TODAY, close=10_000.0, source="brvm_org"),
    ])
    bonds_repo.upsert_snapshots(conn, [
        BondSnapshot(
            ticker="EOM.O10", session_date=TODAY,
            accrued_coupon=441.64,
            last_coupon_date=date(2025, 12, 9),
            last_coupon_amount=620.0,
            source="brvm_org",
        ),
    ])


class TestGetBondView:
    def test_returns_none_for_missing_ticker(self, monkeypatch, tmp_path):
        db_path = tmp_path / "brvm.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        from brvm.config import reset_settings_cache
        reset_settings_cache()
        _apply_migrations(db_path)
        assert bonds_svc.get_bond_view("NOPE") is None

    def test_returns_none_for_equity(self, monkeypatch, tmp_path):
        db_path = tmp_path / "brvm.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        from brvm.config import reset_settings_cache
        reset_settings_cache()
        _apply_migrations(db_path)
        with connect(db_path) as conn:
            sec_repo.upsert(conn, [Security(ticker="SNTS", name="SONATEL", kind="equity")])
        assert bonds_svc.get_bond_view("SNTS") is None

    def test_composes_reference_snapshot_schedule_yield(self, monkeypatch, tmp_path):
        db_path = tmp_path / "brvm.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        from brvm.config import reset_settings_cache
        reset_settings_cache()
        _apply_migrations(db_path)
        with connect(db_path) as conn:
            _seed_mali(conn)
        view = bonds_svc.get_bond_view("EOM.O10", today=TODAY)
        assert view is not None
        assert view.coupon_rate == 6.20
        assert view.maturity_year == 2029
        assert view.issuer_name == "ETAT DU MALI"
        assert view.clean_price == 10_000.0
        assert view.last_snapshot is not None
        assert view.last_snapshot.accrued_coupon == 441.64
        assert view.schedule is not None
        assert view.schedule.next_coupon_date == date(2026, 12, 9)
        assert view.yield_ is not None
        assert view.yield_.dirty_price == 10_000.0 + 441.64
        # Even with accrued, current yield stays the coupon / clean-price
        # ratio — that's how the market reads current yield.
        assert view.yield_.current_yield_pct == 6.20

    def test_related_excludes_self_and_sorts_by_maturity(self, monkeypatch, tmp_path):
        db_path = tmp_path / "brvm.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        from brvm.config import reset_settings_cache
        reset_settings_cache()
        _apply_migrations(db_path)
        with connect(db_path) as conn:
            _seed_mali(conn)
        view = bonds_svc.get_bond_view("EOM.O10", today=TODAY)
        assert view is not None
        tickers = [r.ticker for r in view.related]
        assert "EOM.O10" not in tickers
        # EOM.O2 (2024) matures first, then EOM.O11 (2030)
        assert tickers == ["EOM.O2", "EOM.O11"]
        matured = {r.ticker: r.is_matured for r in view.related}
        assert matured["EOM.O2"] is True     # 2024 < 2026
        assert matured["EOM.O11"] is False    # 2030 > 2026

    def test_issuer_equity_cross_link(self, monkeypatch, tmp_path):
        db_path = tmp_path / "brvm.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        from brvm.config import reset_settings_cache
        reset_settings_cache()
        _apply_migrations(db_path)
        with connect(db_path) as conn:
            _seed_mali(conn)
            # Bond issuer_name "ECOBANK CI" should resolve to the equity
            # whose name contains "Ecobank".
            sec_repo.upsert(conn, [
                Security(
                    ticker="ECOC.O1", name="ECOBANK CI 6,50% 2024-2029",
                    kind="bond", sector="Obligations privées",
                    coupon_rate=6.50, maturity_year=2029,
                    issue_date=date(2025, 6, 12),
                    issuer_name="ECOBANK CI",
                ),
                Security(
                    ticker="ETIT", name="ECOBANK TRANSNATIONAL INC",
                    kind="equity", country="CI",
                ),
            ])
        view = bonds_svc.get_bond_view("ECOC.O1", today=TODAY)
        assert view is not None
        assert view.issuer_equity_ticker == "ETIT"

    def test_state_bond_has_no_equity_cross_link(self, monkeypatch, tmp_path):
        db_path = tmp_path / "brvm.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        from brvm.config import reset_settings_cache
        reset_settings_cache()
        _apply_migrations(db_path)
        with connect(db_path) as conn:
            _seed_mali(conn)
        view = bonds_svc.get_bond_view("EOM.O10", today=TODAY)
        assert view is not None
        assert view.issuer_equity_ticker is None
