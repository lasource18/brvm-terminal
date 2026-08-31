"""Tests for the FR/EN locale toggle (PR-E).

Covers the catalog fallback semantics, `normalize` sanitising, and the
end-to-end request flow: `/lang/{code}` sets a cookie, subsequent
requests honour it, `?lang=` query overrides the cookie for that
render only.
"""

from __future__ import annotations

from kodji import i18n
from kodji.apps.web._common import LANG_COOKIE


class TestCatalogFallback:
    def test_returns_translation_when_registered(self):
        assert i18n.translate("Overview", "fr") == "Aperçu"

    def test_returns_source_when_missing(self):
        # A source string not in the FR catalog must fall back to itself
        # — that's what lets new templates ship without breaking any
        # active locale.
        assert i18n.translate("This has no translation on file", "fr") == \
            "This has no translation on file"

    def test_english_is_identity(self):
        assert i18n.translate("Overview", "en") == "Overview"


class TestNormalize:
    def test_supported_codes_passthrough(self):
        assert i18n.normalize("fr") == "fr"
        assert i18n.normalize("en") == "en"

    def test_case_insensitive(self):
        assert i18n.normalize("FR") == "fr"
        assert i18n.normalize("En") == "en"

    def test_region_variant_strips_to_base(self):
        assert i18n.normalize("fr-FR") == "fr"
        assert i18n.normalize("en_US") == "en"

    def test_unknown_falls_back_to_default(self):
        # Bad cookies / hand-crafted query params must not 500 a render.
        assert i18n.normalize("de") == i18n.DEFAULT_LOCALE
        assert i18n.normalize("xx-YY") == i18n.DEFAULT_LOCALE
        assert i18n.normalize("") == i18n.DEFAULT_LOCALE
        assert i18n.normalize(None) == i18n.DEFAULT_LOCALE


class TestLangRoute:
    def test_sets_cookie_and_redirects(self, client):
        r = client.get("/lang/fr?next=/directory", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/directory"
        set_cookie = r.headers.get("set-cookie", "")
        assert f"{LANG_COOKIE}=fr" in set_cookie
        assert "Max-Age=" in set_cookie

    def test_unknown_locale_falls_back_to_default(self, client):
        # /lang/de should still redirect + set cookie to the default,
        # never 4xx — the UI should never present a code it can't accept
        # but hand-crafted URLs still need to degrade cleanly.
        r = client.get("/lang/de?next=/", follow_redirects=False)
        assert r.status_code == 303
        set_cookie = r.headers.get("set-cookie", "")
        assert f"{LANG_COOKIE}={i18n.DEFAULT_LOCALE}" in set_cookie

    def test_rejects_off_origin_next_target(self, client):
        # Prevent open-redirect via crafted `next=` values.
        for evil in ("https://evil.example.com", "//evil.example.com", "javascript:alert(1)"):
            r = client.get(f"/lang/fr?next={evil}", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/"


class TestLocaleRenderFlow:
    def test_default_locale_renders_english(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Overview" in r.text
        assert "Aperçu" not in r.text

    def test_cookie_switches_to_french(self, client):
        # Round-trip: set cookie via /lang/fr, then follow-up request
        # renders French for translated topbar entries.
        client.get("/lang/fr?next=/", follow_redirects=False)
        r = client.get("/")
        assert "Aperçu" in r.text
        assert "MARCHÉ" in r.text  # market badge, FERMÉ or OUVERT

    def test_query_overrides_cookie(self, client):
        # A `?lang=en` on a request from a FR-cookie client should render
        # English for that request without stomping the cookie (the toggle
        # gets to be authoritative; ad-hoc query is a preview).
        client.get("/lang/fr?next=/", follow_redirects=False)
        r = client.get("/?lang=en")
        assert "Overview" in r.text
        # Cookie stays FR for subsequent implicit requests.
        r2 = client.get("/")
        assert "Aperçu" in r2.text

    def test_toggle_appears_in_topbar(self, client):
        r = client.get("/")
        assert 'href="/lang/fr' in r.text
        assert 'href="/lang/en' in r.text
        assert "lang-active" in r.text  # one of the two links is marked active
