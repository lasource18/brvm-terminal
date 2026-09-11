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
from kodji.store import accounts as accounts_repo
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


def test_returning_user_keeps_one_account(anon_client, mailer):
    first = auth_svc.complete_with_token(_challenge(mailer).token)
    second = auth_svc.complete_with_token(_challenge(mailer).token)

    assert first is not None and second is not None
    assert second.account_id == first.account_id
    assert second.user_id == first.user_id
    assert second.token != first.token  # a fresh session each time

    with connect(settings.db_path) as conn:
        n = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        assert n == 1


def test_email_is_normalized(anon_client, mailer):
    grant = auth_svc.complete_with_token(_challenge(mailer, "  Trader@Example.CI ").token)
    assert grant is not None
    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT email FROM users").fetchone()
        assert row["email"] == EMAIL


def test_claimed_owner_signs_in_to_account_1_and_is_paid(client, mailer):
    """End to end for `just claim-owner`: after the claim, a magic link
    for that address lands on the seeded operator account, which 0019
    put on the paid plan — not on a fresh free one."""
    from kodji.store.accounts import PAID_PLAN

    with connect(settings.db_path) as conn:
        accounts_repo.attach_user_to_account(conn, EMAIL, DEFAULT_ACCOUNT_ID)

    grant = auth_svc.complete_with_token(_challenge(mailer).token)
    assert grant is not None
    assert grant.account_id == DEFAULT_ACCOUNT_ID
    assert accounts_svc.plan_for(grant.account_id) == PAID_PLAN


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


# --- global send budget ------------------------------------------------------


def _ask(mailer, email: str):
    return auth_svc.request_login(email, base_url="https://x", mailer=mailer)


def test_global_send_cap_per_hour(client, mailer, monkeypatch):
    """The spray defence: distinct addresses each pass the per-address
    cap; only a total can see them."""
    monkeypatch.setenv("LOGIN_MAX_SENDS_PER_HOUR", "2")
    reset_settings_cache()

    assert _ask(mailer, "a@example.ci").ok
    assert _ask(mailer, "b@example.ci").ok
    third = _ask(mailer, "c@example.ci")

    assert not third.ok
    assert third.note == "capped"
    assert len(mailer.sent) == 2


def test_global_send_cap_per_day(client, mailer, monkeypatch):
    monkeypatch.setenv("LOGIN_MAX_SENDS_PER_HOUR", "100")
    monkeypatch.setenv("LOGIN_MAX_SENDS_PER_DAY", "1")
    reset_settings_cache()

    assert _ask(mailer, "a@example.ci").ok
    assert _ask(mailer, "b@example.ci").note == "capped"
    assert len(mailer.sent) == 1


def test_per_address_limited_requests_do_not_spend_the_global_budget(
    client, mailer, monkeypatch
):
    """One address hammering us is absorbed by its own cap and must not
    push everyone else into the global one."""
    monkeypatch.setenv("LOGIN_MAX_PER_HOUR", "1")
    monkeypatch.setenv("LOGIN_MAX_SENDS_PER_HOUR", "2")
    reset_settings_cache()

    assert _ask(mailer, EMAIL).ok
    for _ in range(5):
        assert _ask(mailer, EMAIL).note == "rate_limited"

    # Budget of 2, one spent: a second address still gets through, a
    # third does not.
    assert _ask(mailer, "b@example.ci").ok
    assert _ask(mailer, "c@example.ci").note == "capped"


def test_purge_keeps_the_last_day_as_the_send_ledger(client, mailer):
    """The daily cap has to see a full day. A spent challenge from two
    hours ago survives the 03:30 purge; one from yesterday does not."""
    from datetime import timedelta

    now = auth_svc.utcnow()
    with connect(settings.db_path) as conn:
        for tag, age_h in (("fresh", 2), ("stale", 30)):
            created = now - timedelta(hours=age_h)
            conn.execute(
                "INSERT INTO login_tokens(token_hash, code_hash, email, locale, "
                "created_utc, expires_utc, consumed_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"h_{tag}", f"c_{tag}", f"{tag}@example.ci", "fr",
                    auth_svc.utc_iso(created),
                    auth_svc.utc_iso(created + timedelta(minutes=20)),
                    auth_svc.utc_iso(created + timedelta(minutes=1)),
                ),
            )
        conn.commit()

    _, tokens = auth_svc.purge_expired()
    assert tokens == 1

    with connect(settings.db_path) as conn:
        left = {r["email"] for r in conn.execute("SELECT email FROM login_tokens")}
        assert left == {"fresh@example.ci"}
        # ...and it still counts toward today's budget.
        day_ago = auth_svc.utc_iso(now - timedelta(hours=24))
        assert auth_repo.count_recent(conn, day_ago) == 1


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


def test_requesting_a_link_creates_no_user(anon_client, mailer):
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


def test_purge_drops_expired_sessions_and_spent_challenges(anon_client, mailer):
    spent = _challenge(mailer)
    grant = auth_svc.complete_with_token(spent.token)
    assert grant is not None
    _challenge(mailer)  # a live one, must survive

    with connect(settings.db_path) as conn:
        conn.execute("UPDATE sessions SET expires_utc = '2020-01-01T00:00:00Z'")
        # A spent challenge is kept for a day as the send ledger (see
        # test_purge_keeps_the_last_day_as_the_send_ledger); age this one
        # past that so the purge is allowed to take it.
        conn.execute(
            "UPDATE login_tokens SET created_utc = '2020-01-01T00:00:00Z' "
            "WHERE consumed_utc IS NOT NULL"
        )
        conn.commit()

    sessions, tokens = auth_svc.purge_expired()
    assert sessions == 1
    assert tokens == 1  # the consumed one; the live challenge stays

    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM login_tokens").fetchone()[0] == 1
