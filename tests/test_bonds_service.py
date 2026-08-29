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

    def test_returns_none_when_forward_window_is_empty(self):
        """F-31: BIDC.O5 (maturity 2026) with a last-coupon anniversary
        already stamped in 2026 has no coupons left to walk forward —
        the schedule builder used to return an empty-but-truthy
        `BondSchedule`, which took the "have schedule" template branch
        and rendered a header-only table without the terminal principal
        row. Returning None sends the tab to the "schedule unavailable"
        branch instead."""
        s = bonds_svc.build_schedule(
            coupon_rate=6.10, maturity_year=2026,
            last_coupon_date=date(2026, 3, 15),  # already past this year's coupon
            issue_date=date(2016, 3, 15),
            today=date(2026, 8, 27),
        )
        # Anchor + 1 year → 2027, which is past maturity year 2026;
        # no rows produced, so the whole schedule bails.
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

    def test_amortizing_bond_at_par_reads_coupon_rate(self):
        """F-09 regression: the fixture row BIDC.O4 (6.10% 2017-2027) is
        an amortizing bond quoted at its residual balance of 1 250 XOF
        (12.5 % of the 10 000 XOF issuance nominal after seven years of
        12.5 %/yr amortization). Threading the residual through as
        `residual_nominal` makes current yield read 6.10 % — the coupon
        rate — rather than the pre-fix 48.80 % that flagged a solvent
        supranational as distressed."""
        y = bonds_svc.current_yield(6.10, 1_250.0, residual_nominal=1_250.0)
        assert y == 6.10

    def test_default_nominal_is_backwards_compatible(self):
        """Callers that don't pass `residual_nominal` still get the
        DEFAULT_NOMINAL_XOF-based calculation — the old behaviour."""
        y = bonds_svc.current_yield(6.10, 1_250.0)
        # Same computation as before: 0.0610 * 10 000 / 1 250 * 100 ≈ 48.80.
        assert y is not None and 48.7 < y < 48.9


class TestInferBondTerms:
    """PR-G: coupon frequency + residual inference from snap x price.

    BRVM doesn't publish coupon frequency structurally, so the ratio
    `price / (last_coupon x 100 / rate)` disambiguates annual from
    semi-annual and quarterly. Amortising annual issues fall out of the
    same rule because price ≈ derived residual under annual cadence.
    """

    def test_annual_full_nominal(self):
        # BOAD.O11-shape: 5.95% annual on 10 000 nominal, coupon 595.
        snap = BondSnapshot(
            ticker="BOADO11", session_date=TODAY,
            last_coupon_amount=595.0, source="brvm_org",
        )
        t = bonds_svc.infer_bond_terms(snap, 5.95, 10_000.0)
        assert t.payments_per_year == 1
        assert abs(t.residual_nominal - 10_000.0) < 1.0
        assert t.confidence == "high"

    def test_semi_annual_full_nominal(self):
        # CRRH.O3-shape: 6.00% semi-annual on 10 000, coupon 300.
        snap = BondSnapshot(
            ticker="CRRHO3", session_date=TODAY,
            last_coupon_amount=300.0, source="brvm_org",
        )
        t = bonds_svc.infer_bond_terms(snap, 6.00, 10_000.0)
        assert t.payments_per_year == 2
        assert abs(t.residual_nominal - 10_000.0) < 1.0
        assert t.confidence == "high"

    def test_annual_amortized(self):
        # BIDC.O4-shape: 6.10% annual on residual 1 250, coupon 76.25.
        snap = BondSnapshot(
            ticker="BIDCO4", session_date=TODAY,
            last_coupon_amount=76.25, source="brvm_org",
        )
        t = bonds_svc.infer_bond_terms(snap, 6.10, 1_250.0)
        assert t.payments_per_year == 1
        assert abs(t.residual_nominal - 1_250.0) < 1.0
        assert t.confidence == "high"

    def test_semi_annual_amortized(self):
        # CRRH.O7-shape: 5.95% semi-annual on residual 2 917, coupon 86.77.
        snap = BondSnapshot(
            ticker="CRRHO7", session_date=TODAY,
            last_coupon_amount=86.77, source="brvm_org",
        )
        t = bonds_svc.infer_bond_terms(snap, 5.95, 2_917.0)
        assert t.payments_per_year == 2
        assert abs(t.residual_nominal - 2_917.0) < 5.0
        assert t.confidence == "high"

    def test_missing_snap_returns_low_confidence_annual(self):
        t = bonds_svc.infer_bond_terms(None, 6.10, 10_000.0)
        assert t.payments_per_year == 1
        assert t.residual_nominal is None
        assert t.confidence == "low"

    def test_missing_price_falls_back_to_annual_shape_residual(self):
        # No price → we can't disambiguate. Return the annual-shape
        # residual (backward-compatible with the pre-inference behaviour)
        # so callers that lack a price still see a sensible schedule.
        snap = BondSnapshot(
            ticker="X", session_date=TODAY,
            last_coupon_amount=300.0, source="brvm_org",
        )
        t = bonds_svc.infer_bond_terms(snap, 6.00, None)
        assert t.payments_per_year == 1
        assert abs(t.residual_nominal - 5_000.0) < 1.0
        assert t.confidence == "low"

    def test_ambiguous_ratio_falls_back_to_annual(self):
        # Ratio 1.55 sits between the annual (≤1.35) and semi-annual
        # (1.70-2.30) bands — return annual + low confidence rather
        # than mis-classify.
        snap = BondSnapshot(
            ticker="X", session_date=TODAY,
            last_coupon_amount=200.0, source="brvm_org",
        )
        # derived_annual = 200 * 100 / 6 = 3333, ratio = 5000/3333 = 1.5
        t = bonds_svc.infer_bond_terms(snap, 6.00, 5_000.0)
        assert t.payments_per_year == 1
        assert t.confidence == "low"


class TestSemiAnnualSchedule:
    def test_semi_annual_walks_six_month_periods_and_halves_coupon(self):
        # 6.00% coupon, semi-annual, matures 2028; anchor 2026-03-15.
        # Expect payments 2026-09, 2027-03, 2027-09, 2028-03, 2028-09
        # (terminal). Coupon per row = 600/2 = 300.
        s = bonds_svc.build_schedule(
            coupon_rate=6.00, maturity_year=2028,
            last_coupon_date=date(2026, 3, 15), issue_date=None,
            today=TODAY, nominal=10_000.0, payments_per_year=2,
        )
        assert s is not None
        assert s.payments_per_year == 2
        assert s.coupons_remaining == 5
        assert s.annual_coupon == 600.0
        assert all(abs(r.coupon - 300.0) < 1e-6 for r in s.rows)
        # Terminal in maturity year, with principal.
        assert s.rows[-1].payment_date.year == 2028
        assert s.rows[-1].principal == 10_000.0
        # All intermediate rows are coupon-only.
        for r in s.rows[:-1]:
            assert r.principal == 0.0

    def test_semi_annual_ytm_at_par_equals_coupon(self):
        # 5-year semi-annual bullet at par: YTM converges to the coupon
        # rate under the days-based discount (small drift from 365.25
        # vs. exact 0.5-year steps; ≤0.001 is well within tolerance).
        rows: list[bonds_svc.CashFlowRow] = []
        nominal = 10_000.0
        coupon = 0.06 / 2 * nominal
        for i in range(1, 11):
            principal = nominal if i == 10 else 0.0
            rows.append(bonds_svc.CashFlowRow(
                payment_date=date(2026, 1, 1),  # date unused by solver
                coupon=coupon, principal=principal,
                total=coupon + principal,
                year_fraction=i * 0.5,
            ))
        y = bonds_svc.solve_ytm(rows, dirty_price=10_000.0)
        assert y is not None
        # Semi-annual convention: bond-equivalent yield ≈ 2 * per-period
        # rate. Solver returns the per-year rate that reproduces the
        # bullet at par → 0.06 within 1e-4.
        assert abs(y - 0.06) < 1e-3

    def test_invalid_ppy_falls_back_to_annual(self):
        # A caller passing an unsupported cadence (e.g. monthly ppy=12)
        # should get an annual schedule, not a crash.
        s = bonds_svc.build_schedule(
            coupon_rate=6.00, maturity_year=2028,
            last_coupon_date=date(2025, 12, 9), issue_date=None,
            today=TODAY, nominal=10_000.0, payments_per_year=12,
        )
        assert s is not None
        assert s.payments_per_year == 1
        assert all(abs(r.coupon - 600.0) < 1e-6 for r in s.rows)


class TestDeriveResidualNominal:
    def test_recovers_residual_from_last_coupon_amount(self):
        """F-09: `last_coupon_amount = residual * coupon / 100`. Given the
        exchange publishes both fields, we can invert it to recover the
        residual outstanding face value for amortizing issues."""
        snap = BondSnapshot(
            ticker="BIDCO4",
            session_date=TODAY,
            last_coupon_amount=76.25,  # 6.10 % of 1 250 residual
            source="brvm_org",
        )
        residual = bonds_svc.derive_residual_nominal(snap, 6.10)
        assert residual is not None
        assert abs(residual - 1_250.0) < 0.01

    def test_returns_none_when_snapshot_or_coupon_missing(self):
        assert bonds_svc.derive_residual_nominal(None, 6.10) is None
        # No last_coupon_amount on the snapshot.
        snap = BondSnapshot(
            ticker="X", session_date=TODAY, source="brvm_org",
        )
        assert bonds_svc.derive_residual_nominal(snap, 6.10) is None
        # A placeholder row with coupon_rate = 0 shouldn't divide-by-zero.
        snap2 = BondSnapshot(
            ticker="X", session_date=TODAY, last_coupon_amount=100.0,
            source="brvm_org",
        )
        assert bonds_svc.derive_residual_nominal(snap2, 0) is None
        assert bonds_svc.derive_residual_nominal(snap2, None) is None


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

    def test_boa_bond_cross_links_to_bank_of_africa_equity(self, monkeypatch, tmp_path):
        """F-32: brand token `BOA` on the bond side never appeared in
        the equities' full names (`BANK OF AFRICA BENIN`, etc.), so a
        naive `%BOA%` LIKE against `securities.name` returned zero
        rows. The synonym expansion maps `BOA` → `BANK OF AFRICA` so
        the sibling equity resolves."""
        db_path = tmp_path / "brvm.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        from brvm.config import reset_settings_cache
        reset_settings_cache()
        _apply_migrations(db_path)
        with connect(db_path) as conn:
            sec_repo.upsert(conn, [
                Security(
                    ticker="BOAB.O1", name="BOA BENIN 6,50% 2024-2029",
                    kind="bond", sector="Obligations privées", country="BJ",
                    coupon_rate=6.50, maturity_year=2029,
                    issue_date=date(2024, 6, 12),
                    issuer_name="BOA BENIN",
                ),
                Security(
                    ticker="BOAB", name="BANK OF AFRICA BENIN",
                    kind="equity", country="BJ",
                ),
            ])
            quotes_repo.upsert_daily_bars(conn, [
                DailyBar(
                    ticker="BOAB.O1", session_date=TODAY,
                    close=10_000.0, source="brvm_org",
                ),
            ])
        view = bonds_svc.get_bond_view("BOAB.O1", today=TODAY)
        assert view is not None
        assert view.issuer_equity_ticker == "BOAB"

    def test_amortizing_bond_current_yield_uses_residual(
        self, monkeypatch, tmp_path
    ):
        """F-09 end-to-end: a BIDC.O4-shape amortizing bond (6.10 %,
        2017-2027, quoted 1 250 XOF) must not render 48.80 % current
        yield. The last-coupon amount recovers the residual, which
        threads into both the schedule and the current-yield formula."""
        db_path = tmp_path / "brvm.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        from brvm.config import reset_settings_cache
        reset_settings_cache()
        _apply_migrations(db_path)
        with connect(db_path) as conn:
            sec_repo.upsert(conn, [
                Security(
                    ticker="BIDCO4",
                    name="BIDC.O4 SUPRA 6.10% 2017-2027",
                    kind="bond", country="CI", sector="Obligations supranationales",
                    coupon_rate=6.10, maturity_year=2027,
                    issue_date=date(2017, 3, 15),
                    issuer_name="BIDC-EBID",
                ),
            ])
            quotes_repo.upsert_daily_bars(conn, [
                DailyBar(
                    ticker="BIDCO4", session_date=TODAY,
                    close=1_250.0, source="brvm_org",
                ),
            ])
            bonds_repo.upsert_snapshots(conn, [
                BondSnapshot(
                    ticker="BIDCO4", session_date=TODAY,
                    accrued_coupon=30.0,
                    last_coupon_date=date(2026, 3, 15),
                    last_coupon_amount=76.25,   # 6.10 % of 1 250 residual
                    source="brvm_org",
                ),
            ])
        view = bonds_svc.get_bond_view("BIDCO4", today=TODAY)
        assert view is not None and view.yield_ is not None
        # At par against the residual, current yield ≈ the coupon rate.
        assert abs(view.yield_.current_yield_pct - 6.10) < 0.05
        # Schedule also picked up the residual as its nominal — the
        # terminal principal row now reflects the balance actually due.
        assert view.schedule is not None
        assert abs(view.schedule.nominal - 1_250.0) < 0.01

    def test_semi_annual_bond_schedule_and_residual(
        self, monkeypatch, tmp_path
    ):
        """PR-G end-to-end: a CRRH.O3-shape 6.00% semi-annual bond priced
        near par (10 000 XOF) with a 300 XOF last coupon should render:
        - schedule.payments_per_year == 2
        - schedule.nominal ≈ 10 000 (the true residual, NOT the halved
          annual-shape 5 000 that the pre-fix code returned)
        - terminal principal row of 10 000 (not 5 000, which is what
          the user reported seeing as "half the current price")
        """
        db_path = tmp_path / "brvm.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        from brvm.config import reset_settings_cache
        reset_settings_cache()
        _apply_migrations(db_path)
        with connect(db_path) as conn:
            sec_repo.upsert(conn, [
                Security(
                    ticker="CRRHO3",
                    name="CRRH-UEMOA 6% 2020-2028",
                    kind="bond", country="TG",
                    sector="Obligations privées",
                    coupon_rate=6.00, maturity_year=2028,
                    issue_date=date(2020, 4, 26),
                    issuer_name="CRRH-UEMOA",
                ),
            ])
            quotes_repo.upsert_daily_bars(conn, [
                DailyBar(
                    ticker="CRRHO3", session_date=TODAY,
                    close=10_000.0, source="brvm_org",
                ),
            ])
            bonds_repo.upsert_snapshots(conn, [
                BondSnapshot(
                    ticker="CRRHO3", session_date=TODAY,
                    accrued_coupon=100.0,
                    last_coupon_date=date(2026, 4, 26),
                    last_coupon_amount=300.0,   # 6% x 10 000 / 2
                    source="brvm_org",
                ),
            ])
        view = bonds_svc.get_bond_view("CRRHO3", today=TODAY)
        assert view is not None
        assert view.schedule is not None
        assert view.schedule.payments_per_year == 2
        assert abs(view.schedule.nominal - 10_000.0) < 1.0
        # Terminal row: principal reflects the true residual, not the
        # halved annual-shape value the pre-fix code showed.
        terminal = view.schedule.rows[-1]
        assert abs(terminal.principal - 10_000.0) < 1.0
        # Every coupon row pays half the annual coupon.
        for r in view.schedule.rows:
            assert abs(r.coupon - 300.0) < 1e-6
        # Current yield at par ≈ coupon rate.
        assert view.yield_ is not None
        assert abs(view.yield_.current_yield_pct - 6.00) < 0.05

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

    def test_prospectus_url_defaults_to_none(self, monkeypatch, tmp_path):
        """No prospectus link seeded → view exposes the field as None so
        the template can hide the row rather than render a broken link."""
        db_path = tmp_path / "brvm.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        from brvm.config import reset_settings_cache
        reset_settings_cache()
        _apply_migrations(db_path)
        with connect(db_path) as conn:
            _seed_mali(conn)
        view = bonds_svc.get_bond_view("EOM.O10", today=TODAY)
        assert view is not None
        assert view.prospectus_url is None

    def test_prospectus_url_is_surfaced_when_set(self, monkeypatch, tmp_path):
        """A pinned prospectus URL on `securities` threads all the way
        through to the view — the Bloomberg-style "Prospectus: <link>"
        row on the bond overview reads this field."""
        db_path = tmp_path / "brvm.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        from brvm.config import reset_settings_cache
        reset_settings_cache()
        _apply_migrations(db_path)
        with connect(db_path) as conn:
            _seed_mali(conn)
            conn.execute(
                "UPDATE securities SET prospectus_url = ? WHERE ticker = ?",
                ("https://example.org/prospectus-eom-o10.pdf", "EOM.O10"),
            )
            conn.commit()
        view = bonds_svc.get_bond_view("EOM.O10", today=TODAY)
        assert view is not None
        assert view.prospectus_url == "https://example.org/prospectus-eom-o10.pdf"

    def test_prospectus_url_seed_matches_admission_communique(
        self, monkeypatch, tmp_path
    ):
        """Re-runs the 0016 backfill UPDATE against a hand-seeded
        news_items + securities pair to prove the WHERE clause picks the
        newest matching prospectus/admission communiqué and pins its URL
        on the correct bond issuer. Guards against the backfill picking
        up unrelated news or the very first (oldest) match."""
        db_path = tmp_path / "brvm.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        from brvm.config import reset_settings_cache
        reset_settings_cache()
        _apply_migrations(db_path)
        from brvm.models import NewsItem
        from brvm.store import news as news_repo

        with connect(db_path) as conn:
            _seed_mali(conn)
            news_repo.upsert_news_items(conn, [
                NewsItem(
                    source="sikafinance", kind="communique",
                    url="https://sikafinance.com/old-admission",
                    url_hash="hash-old-adm",
                    title="ETAT DU MALI — Admission à la cote (2015)",
                    issuer_name="ETAT DU MALI",
                    published_at="2015-06-01T00:00:00+00:00",
                ),
                NewsItem(
                    source="sikafinance", kind="communique",
                    url="https://sikafinance.com/new-obligation",
                    url_hash="hash-new-obl",
                    title="ETAT DU MALI — Obligation 2023-2029",
                    issuer_name="ETAT DU MALI",
                    published_at="2023-02-15T00:00:00+00:00",
                ),
                NewsItem(
                    source="sikafinance", kind="news",
                    url="https://sikafinance.com/unrelated",
                    url_hash="hash-unrelated",
                    title="ETAT DU MALI — Résultat budgétaire trimestriel",
                    issuer_name="ETAT DU MALI",
                    published_at="2024-03-01T00:00:00+00:00",
                ),
            ])
            conn.execute(
                """
                UPDATE securities
                SET prospectus_url = (
                    SELECT n.url
                    FROM news_items AS n
                    WHERE n.issuer_name = securities.issuer_name
                      AND (
                        LOWER(n.title) LIKE '%obligat%' OR
                        LOWER(n.title) LIKE '%cotation%' OR
                        LOWER(n.title) LIKE '%admission%'
                      )
                    ORDER BY COALESCE(n.published_at, n.fetched_utc) DESC
                    LIMIT 1
                )
                WHERE kind = 'bond'
                  AND issuer_name IS NOT NULL
                  AND prospectus_url IS NULL
                """
            )
            conn.commit()
        view = bonds_svc.get_bond_view("EOM.O10", today=TODAY)
        assert view is not None
        # Newest matching communiqué wins; unrelated budget news is
        # filtered out even though it has the newest date overall.
        assert view.prospectus_url == "https://sikafinance.com/new-obligation"
