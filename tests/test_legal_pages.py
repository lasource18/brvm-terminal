"""Terms, privacy and refund policy: public, bilingual, linked from every page."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("page", ["terms", "privacy", "refunds"])
def test_legal_pages_render_in_both_locales(client, page):
    client.cookies.clear()  # public: no session needed
    fr = client.get(f"/legal/{page}?lang=fr")
    en = client.get(f"/legal/{page}?lang=en")
    assert fr.status_code == 200 and en.status_code == 200
    assert "Version du 2026-09-12" in fr.text
    assert "Version dated 2026-09-12" in en.text
    assert "Kodji Terminal" in fr.text
    assert "Abidjan" in en.text


def test_terms_and_refunds_state_the_prices_and_no_auto_renewal(client):
    fr = client.get("/legal/terms?lang=fr").text
    assert "12 000 XOF" in fr and "120 000 XOF" in fr
    assert "sans reconduction automatique" in fr
    en = client.get("/legal/refunds?lang=en").text
    assert "no automatic renewal" in en
    assert "within <strong>7 days</strong>" in en


def test_privacy_names_the_processors_and_session_lifetime(client):
    en = client.get("/legal/privacy?lang=en").text
    for name in ("Vultr", "Cloudflare", "Resend", "Paystack"):
        assert name in en
    assert "expire after 30 days" in en


def test_unknown_legal_page_404s(client):
    assert client.get("/legal/cookies").status_code == 404


def test_footer_links_the_legal_pages_on_every_page(client):
    client.cookies.clear()
    body = client.get("/").text
    for href in ("/legal/terms", "/legal/privacy", "/legal/refunds"):
        assert f'href="{href}"' in body
    assert "Not investment advice" in body
