"""The owner-only audience page, and the crawler handling behind it.

The gate is the part worth being paranoid about: this page aggregates
every visitor's behaviour, and the only thing standing between it and a
signed-in customer is one account-id comparison.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kodji.config import settings
from kodji.db import connect
from kodji.services import analytics
from kodji.store import accounts as accounts_repo
from kodji.store import analytics as repo
from kodji.store.accounts import DEFAULT_ACCOUNT_ID

TODAY = datetime.now(UTC).strftime("%Y-%m-%d")


def _seed(path="/", visitor="alice", n=1, suspected=False, locale="fr", referrer="www.google.com"):
    with connect(settings.db_path) as conn:
        for _ in range(n):
            repo.insert_view(
                conn, ts_utc=f"{TODAY}T10:00:00Z", day=TODAY, path=path, status=200,
                referrer_host=referrer, visitor_hash=visitor, locale=locale,
                signed_in=False, plan=None, is_pwa=False, is_suspected_bot=suspected,
            )


class TestTheGate:
    def test_the_owner_gets_the_page(self, client):
        r = client.get("/ops/stats")
        assert r.status_code == 200
        assert "Audience" in r.text

    def test_a_signed_out_visitor_gets_404_not_403(self, client):
        """403 would confirm the page exists."""
        client.cookies.clear()
        r = client.get("/ops/stats")
        assert r.status_code == 404
        assert "Audience" not in r.text

    def test_another_account_gets_404(self, client):
        """The load-bearing case: a paying customer with a valid session
        is still not the operator."""
        from datetime import timedelta

        from kodji.clock import utc_iso, utcnow
        from kodji.store import auth as auth_repo
        from kodji.store.auth import SESSION_COOKIE, hash_secret, new_token

        token = new_token()
        with connect(settings.db_path) as conn:
            user_id, account_id = accounts_repo.ensure_user_with_account(conn, "customer@example.ci")
            assert account_id != DEFAULT_ACCOUNT_ID
            accounts_repo.set_plan(conn, account_id, "paid")
            auth_repo.create_session(
                conn,
                token_hash=hash_secret(token),
                user_id=user_id,
                account_id=account_id,
                expires_utc=utc_iso(utcnow() + timedelta(days=1)),
            )
            conn.commit()
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, token)
        assert client.get("/ops/stats").status_code == 404
        # ... while a page they are entitled to still works, proving the
        # session itself is good and only the owner check refused them.
        assert client.get("/").status_code == 200

    def test_the_page_is_not_linked_from_anywhere(self, client):
        for path in ("/", "/news", "/directory", "/billing"):
            assert "/ops/stats" not in client.get(path).text

    def test_robots_keeps_crawlers_out_of_ops(self, client):
        assert "Disallow: /ops/" in client.get("/robots.txt").text


class TestThePage:
    def test_headline_figures_and_funnel(self, client):
        _seed(path="/", visitor="alice", n=3)
        _seed(path="/pricing", visitor="alice")
        _seed(path="/", visitor="bob")
        body = client.get("/ops/stats").text
        assert ">5<" in body           # views
        assert ">2<" in body           # visitors
        assert "Reached plans" in body
        assert "50%" in body           # 1 of 2 visitors reached /pricing

    def test_empty_window_does_not_divide_by_zero(self, client):
        r = client.get("/ops/stats")
        assert r.status_code == 200
        assert "Nothing recorded in this window yet." in r.text

    @pytest.mark.parametrize("days", [7, 14, 30, 90])
    def test_window_switcher(self, client, days):
        assert client.get(f"/ops/stats?days={days}").status_code == 200

    @pytest.mark.parametrize("bad", ["999", "-1", "abc", ""])
    def test_a_bad_window_falls_back_rather_than_500ing(self, client, bad):
        r = client.get(f"/ops/stats?days={bad}")
        assert r.status_code in (200, 422)

    def test_french(self, client):
        _seed()
        body = client.get("/ops/stats?lang=fr").text
        assert "Visiteurs" in body
        assert "Par jour" in body
        assert "ni votre navigateur ne sont conservés" in body


class TestSuspectedCrawlersAreVisibleNotHidden:
    def test_suspected_rows_are_excluded_from_the_figures(self, client):
        _seed(visitor="human", n=2)
        _seed(visitor="crawler", n=50, suspected=True)
        s = analytics.summary(days=7)
        assert s.views == 2
        assert s.visitors == 1
        assert s.suspected_views == 50
        assert s.suspected_visitors == 1

    def test_the_exclusion_is_stated_on_the_page(self, client):
        """An invisible filter is how the numbers lie quietly."""
        _seed(visitor="human")
        _seed(visitor="crawler", n=9, suspected=True)
        body = client.get("/ops/stats").text
        assert "Excluded from the figures above:" in body
        assert ">9<" in body

    def test_suspected_rows_stay_out_of_paths_referrers_and_locales(self, client):
        _seed(path="/", visitor="human", locale="fr", referrer=None)
        _seed(path="/spam", visitor="crawler", n=40, suspected=True,
              locale="en", referrer="spam.example")
        s = analytics.summary(days=7)
        assert [p["path"] for p in s.paths] == ["/"]
        assert s.referrers == []
        assert [locale["locale"] for locale in s.locales] == ["fr"]


class TestBehaviouralDetection:
    """Catches crawlers nobody has named yet — the gap that made the
    original name-list filter unable to explain an unexplained view."""

    def test_a_real_browser_is_not_suspected(self):
        assert not analytics.looks_automated(
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            accept_language="fr-CI,fr;q=0.9,en;q=0.8",
        )

    def test_no_accept_language_is_the_strongest_signal(self):
        """curl, python-requests and most scrapers send none; every
        browser sends one."""
        assert analytics.looks_automated(accept="text/html", accept_language="")
        assert analytics.looks_automated(accept="text/html", accept_language="   ")

    def test_not_asking_for_html_is_suspected(self):
        assert analytics.looks_automated(accept="*/*", accept_language="en-US")
        assert analytics.looks_automated(accept="", accept_language="en-US")

    def test_an_older_browser_without_sec_fetch_is_still_a_visitor(self):
        """Sec-Fetch-* is deliberately not required: it is absent on older
        phones, and under-counting this audience is the failure to avoid."""
        assert not analytics.looks_automated(
            accept="text/html,application/xhtml+xml", accept_language="fr",
        )

    def test_flagged_through_the_app_not_dropped(self, client):
        client.get("/", headers={"Accept": "*/*", "Accept-Language": ""})
        with connect(settings.db_path) as conn:
            rows = conn.execute("SELECT is_suspected_bot FROM pageviews").fetchall()
        assert len(rows) == 1, "flagged, not dropped — a dropped row cannot be reviewed"
        assert rows[0]["is_suspected_bot"] == 1

    def test_a_named_bot_is_still_dropped_outright(self, client):
        """High-volume and certain; no value in keeping it."""
        client.get("/", headers={"User-Agent": "Googlebot/2.1", "Accept-Language": "en"})
        with connect(settings.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM pageviews").fetchone()[0] == 0
