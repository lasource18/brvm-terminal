"""PR-X2: magic-link sign-in.

What these pin, in rough order of how much they'd hurt to get wrong:

1. **A GET on a magic link signs nobody in.** Mail scanners prefetch every
   URL in a message; if the GET consumed the challenge the user would meet
   an expired link on one they just received.
2. **Single use, and only for the account it belongs to.** A consumed link
   is dead, and the session it minted resolves to that user's own account —
   which is what makes PR-X's `WHERE account_id` scoping mean anything.
3. **The code path is bounded.** Six digits is guessable by construction,
   so wrong guesses are capped and the challenge burns.
"""

from __future__ import annotations

import pytest

from kodji.config import reset_settings_cache, settings
from kodji.db import connect
from kodji.services import accounts as accounts_svc
from kodji.services import auth as auth_svc
from kodji.services.auth import SESSION_COOKIE
from kodji.services.mailer import ConsoleMailer
from kodji.store import auth as auth_repo
from kodji.store.accounts import DEFAULT_ACCOUNT_ID

EMAIL = "trader@example.ci"


@pytest.fixture()
def mailer():
    """Captures what would have been sent instead of sending it."""
    return ConsoleMailer()


def _challenge(mailer, email: str = EMAIL, **kw):
    result = auth_svc.request_login(
        email, base_url="https://kodji.test", mailer=mailer, **kw
    )
    assert result.ok, result.note
    return result


# --- the happy paths -------------------------------------------------------


def test_link_signs_in_and_creates_the_account(client, mailer):
    challenge = _challenge(mailer)
    grant = auth_svc.complete_with_token(challenge.token)

    assert grant is not None
    assert grant.account_id > 0
    with connect(settings.db_path) as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE lower(email) = ?", (EMAIL,)
        ).fetchone()
        assert user is not None
        assert user["id"] == grant.user_id


def test_code_signs_in_too(client, mailer):
    challenge = _challenge(mailer)
    grant = auth_svc.complete_with_code(EMAIL, challenge.code)
    assert grant is not None


def test_returning_user_keeps_one_account(client, mailer):
    first = auth_svc.complete_with_token(_challenge(mailer).token)
    second = auth_svc.complete_with_token(_challenge(mailer).token)

    assert first is not None and second is not None
    assert second.account_id == first.account_id
    assert second.user_id == first.user_id
    assert second.token != first.token  # a fresh session each time

    with connect(settings.db_path) as conn:
        n = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        assert n == 1


def test_email_is_normalized(client, mailer):
    grant = auth_svc.complete_with_token(_challenge(mailer, "  Trader@Example.CI ").token)
    assert grant is not None
    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT email FROM users").fetchone()
        assert row["email"] == EMAIL


# --- prefetch safety -------------------------------------------------------


def test_peek_does_not_consume_the_link(client, mailer):
    """The one that matters: a scanner opening the link must not burn it."""
    challenge = _challenge(mailer)

    assert auth_svc.peek_token(challenge.token) == EMAIL
    assert auth_svc.peek_token(challenge.token) == EMAIL  # a second scanner

    assert auth_svc.complete_with_token(challenge.token) is not None


def test_get_on_the_link_route_does_not_consume_it(client, mailer):
    """Same guarantee, asserted through HTTP rather than the service."""
    challenge = _challenge(mailer)

    page = client.get(f"/login/t/{challenge.token}")
    assert page.status_code == 200
    assert EMAIL in page.text
    assert SESSION_COOKIE not in page.cookies

    resp = client.post(f"/login/t/{challenge.token}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.cookies.get(SESSION_COOKIE)


# --- single use and expiry -------------------------------------------------


def test_link_works_once(client, mailer):
    challenge = _challenge(mailer)
    assert auth_svc.complete_with_token(challenge.token) is not None
    assert auth_svc.complete_with_token(challenge.token) is None


def test_using_the_link_kills_the_code(client, mailer):
    """One challenge, two ways in — consuming either spends both."""
    challenge = _challenge(mailer)
    assert auth_svc.complete_with_token(challenge.token) is not None
    assert auth_svc.complete_with_code(EMAIL, challenge.code) is None


def test_expired_link_is_refused(client, mailer, monkeypatch):
    monkeypatch.setenv("LOGIN_TOKEN_TTL_MINUTES", "0")
    reset_settings_cache()
    challenge = _challenge(mailer)

    assert auth_svc.peek_token(challenge.token) is None
    assert auth_svc.complete_with_token(challenge.token) is None
    assert auth_svc.complete_with_code(EMAIL, challenge.code) is None


def test_unknown_token_is_refused(client):
    assert auth_svc.peek_token("not-a-real-token") is None
    assert auth_svc.complete_with_token("not-a-real-token") is None


def test_raw_secrets_are_not_stored(client, mailer):
    """A stolen database must not hand over live links or sessions."""
    challenge = _challenge(mailer)
    auth_svc.complete_with_token(challenge.token)

    with connect(settings.db_path) as conn:
        token_row = conn.execute("SELECT * FROM login_tokens").fetchone()
        session_row = conn.execute("SELECT * FROM sessions").fetchone()

    stored = set(map(str, tuple(token_row) + tuple(session_row)))
    assert challenge.token not in stored
    assert challenge.code not in stored


# --- brute force and abuse -------------------------------------------------


def test_wrong_code_burns_the_challenge_after_the_cap(client, mailer, monkeypatch):
    monkeypatch.setenv("LOGIN_CODE_MAX_ATTEMPTS", "3")
    reset_settings_cache()
    challenge = _challenge(mailer)

    for _ in range(3):
        assert auth_svc.complete_with_code(EMAIL, "000000") is None

    # Even the *right* code is dead now — the challenge was burned.
    assert auth_svc.complete_with_code(EMAIL, challenge.code) is None


def test_rate_limited_per_address(client, mailer, monkeypatch):
    monkeypatch.setenv("LOGIN_MAX_PER_HOUR", "2")
    reset_settings_cache()

    assert auth_svc.request_login(EMAIL, base_url="https://x", mailer=mailer).ok
    assert auth_svc.request_login(EMAIL, base_url="https://x", mailer=mailer).ok
    third = auth_svc.request_login(EMAIL, base_url="https://x", mailer=mailer)

    assert not third.ok
    assert third.note == "rate_limited"
    assert len(mailer.sent) == 2  # the third was never sent

    # A different address is unaffected.
    assert auth_svc.request_login("other@example.ci", base_url="https://x",
                                  mailer=mailer).ok


def test_a_new_request_supersedes_the_previous_code(client, mailer):
    """Only the newest live challenge answers to a typed code, so an old
    mail sitting in an inbox stops being a credential."""
    first = _challenge(mailer)
    second = _challenge(mailer)

    assert auth_svc.complete_with_code(EMAIL, first.code) is None
    assert auth_svc.complete_with_code(EMAIL, second.code) is not None


def test_bad_addresses_are_rejected_before_anything_is_stored(client, mailer):
    for bad in ["", "   ", "nope", "no@domain", "two@@at.ci", "a b@c.ci"]:
        result = auth_svc.request_login(bad, base_url="https://x", mailer=mailer)
        assert not result.ok, bad
        assert result.note == "invalid_email", bad

    assert mailer.sent == []
    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM login_tokens").fetchone()[0] == 0


def test_requesting_a_link_creates_no_user(client, mailer):
    """Enumeration probes must not populate the users table — the account
    is created when a challenge is consumed, not when one is asked for."""
    _challenge(mailer)
    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0


# --- sessions --------------------------------------------------------------


def test_session_cookie_resolves_to_the_users_own_account(client, mailer):
    grant = auth_svc.complete_with_token(_challenge(mailer).token)
    assert grant is not None

    request = type("Req", (), {"cookies": {SESSION_COOKIE: grant.token}})()
    identity = accounts_svc.identity_for(request)

    assert identity is not None
    assert identity.account_id == grant.account_id
    assert identity.email == EMAIL
    assert accounts_svc.current_account_id(request) == grant.account_id
    # A brand-new user is NOT the account the migration seeded.
    assert grant.account_id != DEFAULT_ACCOUNT_ID


def test_signed_out_request_falls_back_while_auth_is_optional(client):
    request = type("Req", (), {"cookies": {}})()
    assert accounts_svc.identity_for(request) is None
    assert accounts_svc.current_account_id(request) == DEFAULT_ACCOUNT_ID


def test_auth_required_refuses_an_anonymous_request(client, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    reset_settings_cache()
    request = type("Req", (), {"cookies": {}})()

    with pytest.raises(accounts_svc.NotAuthenticated):
        accounts_svc.current_account_id(request)

    # A local process (TUI, scheduled job) still resolves — it has no
    # cookie to present and nowhere to be redirected to.
    assert accounts_svc.current_account_id() == DEFAULT_ACCOUNT_ID


def test_logout_kills_the_session(client, mailer):
    grant = auth_svc.complete_with_token(_challenge(mailer).token)
    assert grant is not None
    request = type("Req", (), {"cookies": {SESSION_COOKIE: grant.token}})()

    assert auth_svc.logout(grant.token) is True
    assert accounts_svc.identity_for(request) is None
    assert auth_svc.logout(grant.token) is False


def test_expired_session_stops_resolving(client, mailer):
    grant = auth_svc.complete_with_token(_challenge(mailer).token)
    assert grant is not None
    with connect(settings.db_path) as conn:
        conn.execute(
            "UPDATE sessions SET expires_utc = '2020-01-01T00:00:00Z' "
            "WHERE token_hash = ?",
            (auth_repo.hash_secret(grant.token),),
        )
        conn.commit()

    request = type("Req", (), {"cookies": {SESSION_COOKIE: grant.token}})()
    assert accounts_svc.identity_for(request) is None


def test_purge_drops_expired_sessions_and_spent_challenges(client, mailer):
    grant = auth_svc.complete_with_token(_challenge(mailer).token)
    assert grant is not None
    _challenge(mailer)  # a live one, must survive

    with connect(settings.db_path) as conn:
        conn.execute("UPDATE sessions SET expires_utc = '2020-01-01T00:00:00Z'")
        conn.commit()

    sessions, tokens = auth_svc.purge_expired()
    assert sessions == 1
    assert tokens == 1  # the consumed one; the live challenge stays

    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM login_tokens").fetchone()[0] == 1
