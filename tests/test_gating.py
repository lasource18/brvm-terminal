"""Plan gating (PR-Y).

The important test here is `TestNoPaidRouteLeaks`: it walks every
`TabSpec` with `min_plan == "paid"` across all three route families —
page, HTMX fragment, JSON API — and asserts a free caller is refused on
each. Hiding a tab from the tabbar is presentation; these are the URLs
that stay typeable, and this is what catches the day someone adds a paid
tab and forgets the guard.
"""

from __future__ import annotations

import pytest

from kodji.apps.web import tabs
from kodji.config import settings
from kodji.db import connect
from kodji.services import watchlist as wl_svc
from kodji.store import accounts as accounts_repo
from kodji.store.accounts import DEFAULT_ACCOUNT_ID

PAID = 402


@pytest.fixture
def free(client):
    """Put the default account — the one an anonymous request resolves
    to — back on the free plan, undoing migration 0019 for this test."""
    with connect(settings.db_path) as conn:
        accounts_repo.set_plan(conn, DEFAULT_ACCOUNT_ID, "free")
    return client


@pytest.fixture
def paid(client):
    with connect(settings.db_path) as conn:
        accounts_repo.set_plan(conn, DEFAULT_ACCOUNT_ID, "paid")
    return client


def _seed_security(ticker="SNTS", kind="equity"):
    """Go through the repository, not raw SQL — `securities` has NOT NULL
    columns (currency, first_seen_utc, active) that `upsert` fills."""
    from kodji.models import Security
    from kodji.store import securities as sec_repo

    with connect(settings.db_path) as conn:
        sec_repo.upsert(conn, [Security(ticker=ticker, name=ticker, kind=kind)])


class TestTabVisibility:
    def test_paid_tabs_are_hidden_from_the_free_tabbar(self):
        keys = {t.key for t in tabs.visible_for("equity", "free")}
        assert not (keys & tabs.paid_keys())

    def test_paid_tabs_are_present_for_paid(self):
        keys = {t.key for t in tabs.visible_for("equity", "paid")}
        assert tabs.paid_keys() & keys == {
            k for k in tabs.paid_keys()
            if "equity" not in tabs.get(k).hidden_for_kinds
        }

    def test_free_tabs_survive_both_plans(self):
        free_keys = {t.key for t in tabs.visible_for("equity", "free")}
        assert "news" in free_keys
        assert "description" in free_keys
        assert "corporate-actions" in free_keys

    def test_free_plan_lands_on_a_tab_it_can_see(self):
        # `chart` is paid, so a bare /s/{ticker} must not redirect a free
        # reader straight into a wall.
        assert tabs.default_tab_for("equity", "free") == "news"
        assert tabs.default_tab_for("equity", "paid") == "chart"
        # Bonds land on their own free overview either way.
        assert tabs.default_tab_for("bond", "free") == "overview"


class TestNoPaidRouteLeaks:
    """Every paid tab, every route family, refused on the free plan."""

    @pytest.mark.parametrize("key", sorted(tabs.paid_keys()))
    def test_page_route_is_refused(self, free, key):
        spec = tabs.get(key)
        kind = "bond" if "bond" in spec.template else "equity"
        ticker = "BOAD.O11" if kind == "bond" else "SNTS"
        _seed_security(ticker, kind)
        r = free.get(f"/s/{ticker}/{key}")
        assert r.status_code == PAID, f"{key} leaked: {r.status_code}"

    @pytest.mark.parametrize("key", sorted(tabs.paid_keys()))
    def test_htmx_request_is_refused(self, free, key):
        spec = tabs.get(key)
        kind = "bond" if "bond" in spec.template else "equity"
        ticker = "BOAD.O11" if kind == "bond" else "SNTS"
        _seed_security(ticker, kind)
        r = free.get(f"/s/{ticker}/{key}", headers={"HX-Request": "true"})
        assert r.status_code == PAID, f"{key} leaked over HTMX: {r.status_code}"

    def test_history_api_is_refused(self, free):
        _seed_security()
        r = free.get("/api/history/SNTS")
        assert r.status_code == PAID
        assert r.json()["error"] == "payment_required"

    def test_history_api_serves_a_paid_caller(self, paid):
        _seed_security()
        r = paid.get("/api/history/SNTS")
        assert r.status_code == 200

    @pytest.mark.parametrize("path", ["/brief", "/alerts"])
    def test_paid_pages_are_refused(self, free, path):
        assert free.get(path).status_code == PAID

    def test_alert_fragments_are_refused(self, free):
        # The page is guarded, but each control on it posts to its own
        # URL that curl can reach directly.
        assert free.post(
            "/_frag/alerts/rules",
            data={"kind": "price_move", "threshold_pct": "5"},
        ).status_code == PAID
        assert free.post("/_frag/alerts/rules/1/toggle").status_code == PAID
        assert free.delete("/_frag/alerts/rules/1").status_code == PAID

    def test_a_refused_alert_rule_is_not_created(self, free):
        free.post(
            "/_frag/alerts/rules",
            data={"kind": "price_move", "threshold_pct": "5"},
        )
        with connect(settings.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0] == 0


class TestFreeSurfaceStaysFree:
    """Gating the computed layer must not gate the scraped facts."""

    @pytest.mark.parametrize("path", ["/", "/news", "/directory", "/watchlists", "/pricing"])
    def test_free_pages_render(self, free, path):
        assert free.get(path).status_code == 200

    def test_pricing_is_reachable_on_free(self, free):
        # It is the destination of every upgrade link; paywalling it
        # would be a loop.
        r = free.get("/pricing")
        assert r.status_code == 200
        assert "kodji" in r.text.lower()

    def test_topbar_hides_paid_links_on_free(self, free):
        body = free.get("/").text
        assert 'href="/alerts"' not in body
        assert 'href="/brief"' not in body
        assert 'href="/pricing"' in body

    def test_topbar_shows_paid_links_on_paid(self, paid):
        body = paid.get("/").text
        assert 'href="/alerts"' in body
        assert 'href="/brief"' in body


class TestWatchlistCap:
    def _add(self, n, plan="free"):
        for i in range(n):
            self._add_one(f"TK{i:02d}", plan=plan)

    @pytest.fixture(autouse=True)
    def _list(self, client):
        wl_svc.create(DEFAULT_ACCOUNT_ID, "Main")

    def test_free_plan_stops_at_the_limit(self):
        self._add(wl_svc.FREE_WATCHLIST_LIMIT)
        with pytest.raises(wl_svc.WatchlistLimitReached) as e:
            self._add_one("EXTRA")
        assert e.value.limit == wl_svc.FREE_WATCHLIST_LIMIT

    def _add_one(self, ticker, plan="free", slug="main"):
        _seed_security(ticker)
        wl_svc.add_item(DEFAULT_ACCOUNT_ID, slug, ticker, plan=plan)

    def test_paid_plan_is_uncapped(self):
        self._add(wl_svc.FREE_WATCHLIST_LIMIT, plan="paid")
        self._add_one("EXTRA", plan="paid")  # must not raise
        assert wl_svc.distinct_ticker_count(DEFAULT_ACCOUNT_ID) == (
            wl_svc.FREE_WATCHLIST_LIMIT + 1
        )

    def test_the_cap_counts_distinct_tickers_not_rows(self):
        """The same name on a second list must not cost a second slot —
        the cap limits breadth, not how the reader organises."""
        wl_svc.create(DEFAULT_ACCOUNT_ID, "Second")
        self._add(wl_svc.FREE_WATCHLIST_LIMIT)
        self._add_one("TK00", slug="second")
        assert wl_svc.distinct_ticker_count(DEFAULT_ACCOUNT_ID) == (
            wl_svc.FREE_WATCHLIST_LIMIT
        )

    def test_route_returns_402_and_a_notice(self, free):
        self._add(wl_svc.FREE_WATCHLIST_LIMIT)
        _seed_security("EXTRA")
        r = free.post("/_frag/watchlists/main/items", data={"ticker": "EXTRA"})
        assert r.status_code == 402
        assert str(wl_svc.FREE_WATCHLIST_LIMIT) in r.text
