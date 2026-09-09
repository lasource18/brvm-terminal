"""PR-X2: the sign-in flow over HTTP.

`test_auth.py` covers the service; this covers what a browser actually
does — form posts, cookies, redirects, and the topbar.
"""

from __future__ import annotations

import pytest

from kodji.config import reset_settings_cache
from kodji.services.auth import SESSION_COOKIE
from kodji.services.mailer import ConsoleMailer

EMAIL = "trader@example.ci"


@pytest.fixture()
def outbox(monkeypatch):
    """Intercepts the mailer the route resolves, and hands back what it got."""
    mailer = ConsoleMailer()
    monkeypatch.setattr("kodji.services.auth.get_mailer", lambda: mailer)
    return mailer


def _link_and_code(outbox) -> tuple[str, str]:
    """Pull the magic link and code back out of the sent message, the way
    a user reads them out of their mail. The link comes back absolute —
    callers wanting a request path use `_path`."""
    text = outbox.sent[-1].text
    link = next(w for w in text.split() if "/login/t/" in w)
    code = next(w for w in text.split() if w.isdigit() and len(w) == 6)
    return link, code


def _path(link: str) -> str:
    return link[link.index("/login/t/") :]


def _sign_in(client, outbox, email: str = EMAIL) -> None:
    client.post("/login", data={"email": email})
    link, _ = _link_and_code(outbox)
    client.post(_path(link))


def test_login_page_renders(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'action="/login"' in resp.text
    assert 'name="email"' in resp.text


def test_sent_page_states_the_same_expiry_as_the_email(client, outbox):
    """The page and the email must never disagree on the TTL — both read
    `login_token_ttl_minutes`. Checked in both locales, and on the
    wrong-code re-render, which is the moment a user most wants to know
    whether the code they have is still good."""
    en = client.post("/login", data={"email": EMAIL}).text
    assert "expire in 20 minutes" in en

    fr = client.post("/login?lang=fr", data={"email": EMAIL}).text
    assert "expirent dans 20 minutes" in fr

    again = client.post("/login/code", data={"email": EMAIL, "code": "000000"}).text
    assert "20 minutes" in again


def test_global_cap_is_surfaced_honestly(client, outbox, monkeypatch):
    """Unlike the per-address limit, the global cap says so: nothing is
    coming to this mailbox, and "temporarily unavailable" reveals
    nothing about any one address — only that the site is busy."""
    monkeypatch.setenv("LOGIN_MAX_SENDS_PER_HOUR", "1")
    reset_settings_cache()

    assert client.post("/login", data={"email": EMAIL}).status_code == 200

    capped = client.post("/login", data={"email": "other@example.ci"})
    assert capped.status_code == 503
    assert capped.headers["retry-after"] == "600"
    assert "temporarily unavailable" in capped.text
    assert len(outbox.sent) == 1

    fr = client.post("/login?lang=fr", data={"email": "third@example.ci"})
    assert fr.status_code == 503
    assert "momentanément indisponible" in fr.text


def test_submitting_an_address_sends_the_mail(client, outbox):
    resp = client.post("/login", data={"email": EMAIL})

    assert resp.status_code == 200
    assert EMAIL in resp.text
    assert len(outbox.sent) == 1
    assert outbox.sent[0].to == EMAIL
    # The code form is on the same page — the user never has to navigate
    # back to type it.
    assert 'action="/login/code"' in resp.text


def test_full_link_flow_signs_in(client, outbox):
    client.post("/login", data={"email": EMAIL})
    link, _ = _link_and_code(outbox)
    path = _path(link)

    assert client.get(path).status_code == 200          # confirm page
    assert SESSION_COOKIE not in client.cookies         # GET signed nobody in

    resp = client.post(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert client.cookies.get(SESSION_COOKIE)

    # The topbar now knows who this is.
    assert EMAIL in client.get("/").text


def test_full_code_flow_signs_in(client, outbox):
    client.post("/login", data={"email": EMAIL})
    _, code = _link_and_code(outbox)

    resp = client.post(
        "/login/code", data={"email": EMAIL, "code": code}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert client.cookies.get(SESSION_COOKIE)


def test_wrong_code_re_renders_with_an_error(client, outbox):
    client.post("/login", data={"email": EMAIL})

    resp = client.post("/login/code", data={"email": EMAIL, "code": "000000"})
    assert resp.status_code == 400
    assert SESSION_COOKIE not in client.cookies
    # The retry form is still there, still carrying the address.
    assert 'action="/login/code"' in resp.text


def test_invalid_address_is_rejected_without_sending(client, outbox):
    resp = client.post("/login", data={"email": "nope"})
    assert resp.status_code == 400
    assert outbox.sent == []


def test_rate_limited_address_gets_the_same_page_as_a_success(client, outbox, monkeypatch):
    """Saying 'too many requests' would confirm to a stranger that someone
    has been asking for links to this address."""
    monkeypatch.setenv("LOGIN_MAX_PER_HOUR", "1")
    reset_settings_cache()

    first = client.post("/login", data={"email": EMAIL})
    second = client.post("/login", data={"email": EMAIL})

    assert first.status_code == second.status_code == 200
    assert first.text == second.text
    assert len(outbox.sent) == 1


def test_session_cookie_is_httponly_and_lax(client, outbox):
    """It is a credential, not a preference — unlike the locale cookie
    next to it, nothing in the UI reads this from JS."""
    client.post("/login", data={"email": EMAIL})
    link, _ = _link_and_code(outbox)
    resp = client.post(_path(link), follow_redirects=False)

    header = resp.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=lax" in header.replace("samesite", "SameSite")


def test_logout_clears_the_session(client, outbox):
    _sign_in(client, outbox)
    assert EMAIL in client.get("/").text

    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert not client.cookies.get(SESSION_COOKIE)
    assert EMAIL not in client.get("/").text


def test_signed_in_user_is_bounced_off_the_login_page(client, outbox):
    _sign_in(client, outbox)

    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_public_base_url_wins_over_the_host_header(client, outbox, monkeypatch):
    """Behind Cloudflare + Caddy the request's host is whatever the last
    proxy claimed. A link built from a spoofed Host would send a live
    credential to someone else's domain."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://kodji.ci")
    reset_settings_cache()

    client.post("/login", data={"email": EMAIL},
                headers={"Host": "evil.example.com"})
    link, _ = _link_and_code(outbox)
    assert link.startswith("https://kodji.ci/login/t/")


def test_topbar_offers_sign_in_when_signed_out(client):
    body = client.get("/").text
    assert 'href="/login"' in body


def test_two_signed_in_users_do_not_see_each_others_watchlists(client, outbox):
    """The whole point of PR-X's account scoping, end to end: two real
    sessions, two accounts, no bleed. Both users create a list with the
    same name — which the pre-PR-X global UNIQUE on slug forbade."""
    _sign_in(client, outbox, "first@example.ci")
    client.post("/_frag/watchlists", data={"name": "Banks"})
    assert "Banks" in client.get("/watchlists").text
    client.post("/logout")

    _sign_in(client, outbox, "second@example.ci")
    second = client.get("/watchlists")
    assert "Banks" not in second.text

    client.post("/_frag/watchlists", data={"name": "Banks"})
    assert "Banks" in client.get("/watchlists").text


# --- login CSRF ------------------------------------------------------------


def test_cross_origin_post_is_refused(client, outbox):
    """Login CSRF: an attacker's page auto-submits *their* magic link into
    a victim's browser, and everything the victim then saves lands in the
    attacker's account. SameSite doesn't help — the damaging request
    carries no cookie of ours at all."""
    client.post("/login", data={"email": EMAIL})
    link, code = _link_and_code(outbox)
    evil = {"origin": "https://evil.example.com"}

    assert client.post(_path(link), headers=evil).status_code == 403
    assert client.post("/login/code", data={"email": EMAIL, "code": code},
                       headers=evil).status_code == 403
    assert client.post("/login", data={"email": EMAIL}, headers=evil).status_code == 403
    assert client.post("/logout", headers=evil).status_code == 403
    assert SESSION_COOKIE not in client.cookies

    # The challenge was never spent, so the real form still works.
    assert client.post(_path(link), follow_redirects=False).status_code == 303


def test_same_origin_post_is_allowed(client, outbox):
    client.post("/login", data={"email": EMAIL})
    link, _ = _link_and_code(outbox)

    resp = client.post(_path(link), headers={"origin": "http://testserver"},
                       follow_redirects=False)
    assert resp.status_code == 303


def test_request_host_is_same_origin_even_under_a_production_base_url(
    client, outbox, monkeypatch
):
    """A production `.env` on a laptop, or the live box through an SSH
    tunnel: the browser's Origin is the host it typed, not
    PUBLIC_BASE_URL. That is still same-origin — the attacker case is
    Origin != Host, never Origin == Host — so sign-in and sign-out must
    work, and only a genuinely foreign Origin is refused."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://kodji.app")
    reset_settings_cache()
    here = {"origin": "http://testserver"}

    client.post("/login", data={"email": EMAIL}, headers=here)
    link, _ = _link_and_code(outbox)
    assert client.post(_path(link), headers=here, follow_redirects=False).status_code == 303
    assert SESSION_COOKIE in client.cookies

    evil = client.post("/logout", headers={"origin": "https://evil.example.com"})
    assert evil.status_code == 403
    # The refusal names what it wanted, for the operator reading it.
    assert "https://kodji.app" in evil.text
    assert SESSION_COOKIE in client.cookies

    assert client.post("/logout", headers=here, follow_redirects=False).status_code == 303
    assert SESSION_COOKIE not in client.cookies
