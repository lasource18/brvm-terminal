"""First-party pageview counting.

Two things these tests exist to hold in place. First, that the counter
records pages and only pages — a fragment or a health check counted as a
pageview makes every number downstream a lie. Second, and more
importantly, that the privacy properties are structural rather than a
promise: no IP address or user agent reaches the database, a referring
URL is reduced to its host, and yesterday's visitor hashes cannot be
re-derived once the salt has rotated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kodji.config import settings
from kodji.db import connect
from kodji.services import analytics
from kodji.store import analytics as repo


class TestWhatCounts:
    @pytest.mark.parametrize(
        "path", ["/", "/news", "/pricing", "/s/SNTS/chart", "/legal/terms"]
    )
    def test_pages_count(self, path):
        assert analytics.should_record(
            path=path, method="GET", status=200, content_type="text/html; charset=utf-8",
            is_htmx=False,
        )

    @pytest.mark.parametrize(
        "path",
        [
            "/health",                    # the uptime monitor, every 5 minutes
            "/favicon.ico",
            "/sw.js",
            "/manifest.webmanifest",
            "/static/style.css",
            "/api/history/SNTS",
            "/_frag/news",
            "/lang/fr",
            "/billing/webhook/paystack",
        ],
    )
    def test_non_pages_do_not_count(self, path):
        assert not analytics.should_record(
            path=path, method="GET", status=200, content_type="text/html", is_htmx=False,
        )

    def test_htmx_fragments_do_not_count(self):
        """A page that auto-refreshes would otherwise look like the
        most-read thing on the site."""
        assert not analytics.should_record(
            path="/", method="GET", status=200, content_type="text/html", is_htmx=True,
        )

    @pytest.mark.parametrize("method", ["POST", "DELETE", "HEAD"])
    def test_only_get(self, method):
        assert not analytics.should_record(
            path="/", method=method, status=200, content_type="text/html", is_htmx=False,
        )

    @pytest.mark.parametrize("status", [302, 401, 402, 404, 500])
    def test_only_success(self, status):
        assert not analytics.should_record(
            path="/", method="GET", status=status, content_type="text/html", is_htmx=False,
        )

    def test_only_html(self):
        assert not analytics.should_record(
            path="/", method="GET", status=200, content_type="application/json", is_htmx=False,
        )

    @pytest.mark.parametrize(
        "ua",
        ["Googlebot/2.1", "curl/8.4.0", "python-requests/2.32", "UptimeRobot/2.0",
         "Mozilla/5.0 (compatible; AhrefsBot/7.0)", "GPTBot/1.0", ""],
    )
    def test_bots_are_filtered(self, ua):
        assert analytics.is_bot(ua)

    def test_a_real_browser_is_not_a_bot(self):
        assert not analytics.is_bot(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15"
        )


class TestReferrer:
    def test_host_only_never_the_path(self):
        """A referring URL can carry search terms or a private path."""
        assert analytics.referrer_host(
            "https://www.google.com/search?q=cours+sonatel+brvm"
        ) == "www.google.com"

    def test_userinfo_is_stripped(self):
        assert analytics.referrer_host("https://user:pw@example.com/x") == "example.com"

    def test_absent_or_unparseable(self):
        assert analytics.referrer_host(None) is None
        assert analytics.referrer_host("") is None
        assert analytics.referrer_host("not a url") is None

    def test_same_origin_is_dropped(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://kodji.app")
        from kodji.config import reset_settings_cache

        reset_settings_cache()
        assert analytics.referrer_host("https://kodji.app/news") is None
        assert analytics.referrer_host("https://sikafinance.com/x") == "sikafinance.com"
        reset_settings_cache()


class TestClientIp:
    def test_cloudflare_header_wins(self):
        """Behind Cloudflare the socket address is an edge node, which
        would collapse whole regions onto one visitor hash."""
        assert analytics.client_ip(
            {"cf-connecting-ip": "41.66.1.2", "x-forwarded-for": "10.0.0.1"}, "172.16.0.9"
        ) == "41.66.1.2"

    def test_forwarded_for_takes_the_client_not_the_proxies(self):
        assert analytics.client_ip(
            {"x-forwarded-for": "41.66.1.2, 10.0.0.1, 10.0.0.2"}, None
        ) == "41.66.1.2"

    def test_falls_back_to_the_socket(self):
        assert analytics.client_ip({}, "127.0.0.1") == "127.0.0.1"
        assert analytics.client_ip({}, None) == "-"


def _views(db_path):
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM pageviews ORDER BY id").fetchall()


class TestPrivacyIsStructural:
    def test_the_table_cannot_hold_an_ip_or_a_user_agent(self, client):
        """Not a promise in a docstring — there is nowhere to put them."""
        with connect(settings.db_path) as conn:
            cols = {r["name"].lower() for r in conn.execute("PRAGMA table_info(pageviews)")}
        for forbidden in ("ip", "ip_address", "user_agent", "ua", "email", "account_id", "user_id"):
            assert forbidden not in cols

    def test_the_visitor_hash_does_not_contain_the_inputs(self, client):
        client.get("/", headers={"User-Agent": "Mozilla/5.0 (X11; Linux)",
                                 "CF-Connecting-IP": "41.66.1.2"})
        rows = _views(settings.db_path)
        assert len(rows) == 1
        blob = " ".join(str(v) for v in tuple(rows[0]))
        assert "41.66.1.2" not in blob
        assert "Mozilla" not in blob

    def test_only_one_salt_row_ever_exists(self, client):
        """A second row would keep a previous day's salt alive, and with it
        the ability to re-derive that day's hashes from an IP."""
        with connect(settings.db_path) as conn:
            analytics._salt_for(conn, "2026-09-10")
            analytics.reset_salt_cache()
            analytics._salt_for(conn, "2026-09-11")
            analytics.reset_salt_cache()
            analytics._salt_for(conn, "2026-09-12")
            rows = conn.execute("SELECT day FROM analytics_salt").fetchall()
        assert len(rows) == 1
        assert rows[0]["day"] == "2026-09-12"

    def test_the_same_visitor_is_one_hash_within_a_day(self, client):
        for _ in range(3):
            client.get("/", headers={"User-Agent": "Mozilla/5.0", "CF-Connecting-IP": "41.66.1.2"})
        rows = _views(settings.db_path)
        assert len(rows) == 3
        assert len({r["visitor_hash"] for r in rows}) == 1

    def test_different_visitors_are_different_hashes(self, client):
        client.get("/", headers={"User-Agent": "Mozilla/5.0", "CF-Connecting-IP": "41.66.1.2"})
        client.get("/", headers={"User-Agent": "Mozilla/5.0", "CF-Connecting-IP": "41.66.9.9"})
        assert len({r["visitor_hash"] for r in _views(settings.db_path)}) == 2

    def test_rotating_the_salt_breaks_the_link_across_days(self, client):
        """The same person on two days must not be joinable."""
        with connect(settings.db_path) as conn:
            analytics._salt_for(conn, "2026-09-10")
            a = analytics._visitor_hash(
                analytics._salt_for(conn, "2026-09-10"), "41.66.1.2", "UA", "2026-09-10"
            )
            analytics.reset_salt_cache()
            b = analytics._visitor_hash(
                analytics._salt_for(conn, "2026-09-11"), "41.66.1.2", "UA", "2026-09-11"
            )
        assert a != b


class TestRecordingThroughTheApp:
    def test_a_page_render_is_counted(self, client):
        client.get("/news")
        rows = _views(settings.db_path)
        assert len(rows) == 1
        assert rows[0]["path"] == "/news"
        assert rows[0]["status"] == 200
        assert rows[0]["locale"] == "en"        # the fixture pins Accept-Language
        assert rows[0]["signed_in"] == 1
        assert rows[0]["plan"] == "paid"

    def test_a_signed_out_visitor_carries_no_plan(self, client):
        client.cookies.clear()
        client.get("/")
        row = _views(settings.db_path)[0]
        assert row["signed_in"] == 0
        assert row["plan"] is None

    def test_the_language_served_is_recorded(self, client):
        """So the Accept-Language negotiation can be checked against what
        visitors actually got."""
        client.get("/", headers={"Accept-Language": "fr-CI,fr;q=0.9"})
        assert _views(settings.db_path)[0]["locale"] == "fr"

    def test_a_query_string_is_not_stored(self, client):
        client.get("/news?ticker=SNTS&from=2026-01-01")
        assert _views(settings.db_path)[0]["path"] == "/news"

    def test_an_installed_app_launch_is_marked(self, client):
        client.get("/?source=pwa")
        assert _views(settings.db_path)[0]["is_pwa"] == 1

    def test_assets_fragments_and_health_are_not_counted(self, client):
        client.get("/health")
        client.get("/favicon.ico")
        client.get("/sw.js")
        client.get("/manifest.webmanifest")
        client.get("/static/style.css")
        client.get("/_frag/news", headers={"HX-Request": "true"})
        assert _views(settings.db_path) == []

    def test_a_crawler_is_not_counted(self, client):
        client.get("/", headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
        assert _views(settings.db_path) == []

    def test_counting_never_breaks_a_page(self, client, monkeypatch):
        """If the counter throws, the reader must still get their page."""
        def boom(*a, **k):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(analytics, "_today", boom)
        r = client.get("/")
        assert r.status_code == 200
        assert "kodji-terminal" in r.text
        assert _views(settings.db_path) == []


class TestSummaryAndPrune:
    def _seed(self, db_path, day, path, visitor, n=1):
        with connect(db_path) as conn:
            for _ in range(n):
                repo.insert_view(
                    conn, ts_utc=f"{day}T10:00:00Z", day=day, path=path, status=200,
                    referrer_host="www.google.com", visitor_hash=visitor,
                    locale="fr", signed_in=False, plan=None, is_pwa=False,
                )

    def test_summary_counts_views_and_distinct_visitors(self, client):
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        self._seed(settings.db_path, today, "/", "alice", n=3)
        self._seed(settings.db_path, today, "/pricing", "alice")
        self._seed(settings.db_path, today, "/", "bob", n=2)

        s = analytics.summary(days=7)
        assert s.views == 6
        assert s.visitors == 2
        assert s.paths[0]["path"] == "/"
        assert s.paths[0]["views"] == 5
        assert s.referrers[0]["referrer_host"] == "www.google.com"
        assert s.locales[0]["locale"] == "fr"
        assert s.pricing_visitors == 1

    def test_summary_window_excludes_older_days(self, client):
        today = datetime.now(UTC)
        old = (today - timedelta(days=40)).strftime("%Y-%m-%d")
        self._seed(settings.db_path, old, "/", "ghost")
        self._seed(settings.db_path, today.strftime("%Y-%m-%d"), "/", "alice")
        assert analytics.summary(days=7).views == 1

    def test_summary_on_an_empty_table(self, client):
        s = analytics.summary(days=7)
        assert (s.views, s.visitors, s.days, s.paths) == (0, 0, [], [])

    def test_prune_drops_only_what_is_past_the_window(self, client):
        now = datetime.now(UTC)
        self._seed(settings.db_path, (now - timedelta(days=400)).strftime("%Y-%m-%d"), "/", "old")
        self._seed(settings.db_path, now.strftime("%Y-%m-%d"), "/", "new")
        assert analytics.prune(retain_days=180) == 1
        rows = _views(settings.db_path)
        assert len(rows) == 1
        assert rows[0]["visitor_hash"] == "new"


class TestStatsRendering:
    """The CLI is the only way these numbers are ever read, so its
    formatting is part of the feature."""

    def test_a_real_zero_is_not_an_em_dash(self):
        """`0 signed in` and `no figure` are different claims."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "kodji_stats", Path(__file__).resolve().parents[1] / "scripts" / "stats.py"
        )
        assert spec and spec.loader
        stats = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stats)

        assert stats._cell(0) == "0"
        assert stats._cell(5) == "5"
        assert stats._cell(None) == "—"
        assert stats._cell("") == "—"
