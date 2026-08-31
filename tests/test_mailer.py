"""PR-X2: the email transport.

The wire format is pinned because it is the one thing here we can't
observe in tests any other way — a wrong field name fails silently as a
422 from Resend, in production, on the one message that matters.
"""

from __future__ import annotations

import httpx
import pytest

from kodji.config import reset_settings_cache, settings
from kodji.services import auth as auth_svc
from kodji.services.mailer import (
    RESEND_ENDPOINT,
    ConsoleMailer,
    EmailMessage,
    ResendMailer,
    get_mailer,
)

MSG = EmailMessage(
    to="trader@example.ci",
    subject="Votre lien de connexion Kodji",
    text="lien",
    html="<p>lien</p>",
)


def _mailer_over(handler) -> ResendMailer:
    return ResendMailer(
        "re_test_key",
        "Kodji <connexion@mail.kodji.test>",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_resend_request_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"id": "msg_123"})

    result = _mailer_over(handler).send(MSG)

    assert result.ok
    assert result.provider_id == "msg_123"
    assert seen["url"] == RESEND_ENDPOINT
    assert seen["auth"] == "Bearer re_test_key"
    assert seen["body"] == {
        "from": "Kodji <connexion@mail.kodji.test>",
        "to": ["trader@example.ci"],
        "subject": MSG.subject,
        "text": MSG.text,
        "html": MSG.html,
    }


def test_plain_text_always_rides_along():
    """A text part is both better-scoring with spam filters and the only
    thing some low-bandwidth mobile clients render."""
    payload = _mailer_over(lambda r: httpx.Response(200, json={})).payload(MSG)
    assert payload["text"] and payload["html"]


@pytest.mark.parametrize(
    ("status", "permanent"),
    [(401, True), (422, True), (429, False), (500, False), (503, False)],
)
def test_failures_are_classified_for_retry(status, permanent):
    """429 is a rate limit, not a rejection — it must not read permanent."""
    result = _mailer_over(lambda r: httpx.Response(status, text="nope")).send(MSG)
    assert not result.ok
    assert result.permanent is permanent


def test_transport_error_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    result = _mailer_over(handler).send(MSG)
    assert not result.ok
    assert not result.permanent
    assert "transport" in result.note


def test_ok_survives_an_unparseable_body():
    """The provider id is for log correlation only; a body we can't read
    must not turn a delivered message into a failed one."""
    result = _mailer_over(lambda r: httpx.Response(200, text="not json")).send(MSG)
    assert result.ok
    assert result.provider_id == ""


def test_console_mailer_when_unconfigured(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setenv("EMAIL_FROM", "")
    reset_settings_cache()
    assert isinstance(get_mailer(), ConsoleMailer)


def test_a_key_without_a_sender_still_falls_back(monkeypatch):
    """Half-configured is a 422 on every send — worse than logging."""
    monkeypatch.setenv("RESEND_API_KEY", "re_live_key")
    monkeypatch.setenv("EMAIL_FROM", "")
    reset_settings_cache()
    assert settings.has_email is False
    assert isinstance(get_mailer(), ConsoleMailer)


def test_resend_mailer_when_configured(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_live_key")
    monkeypatch.setenv("EMAIL_FROM", "Kodji <connexion@mail.kodji.test>")
    reset_settings_cache()
    mailer = get_mailer()
    try:
        assert isinstance(mailer, ResendMailer)
    finally:
        mailer.close()


# --- the message itself ----------------------------------------------------


@pytest.mark.parametrize("locale", ["fr", "en"])
def test_login_email_carries_both_the_link_and_the_code(locale):
    msg = auth_svc.build_login_email(
        "trader@example.ci",
        link="https://kodji.test/login/t/abc",
        code="123456",
        locale=locale,
    )
    for body in (msg.text, msg.html):
        assert "https://kodji.test/login/t/abc" in body
        assert "123456" in body
    assert msg.to == "trader@example.ci"
    assert msg.subject


def test_login_email_is_french_by_default_for_french_readers():
    fr = auth_svc.build_login_email("x@y.ci", link="l", code="1", locale="fr")
    en = auth_svc.build_login_email("x@y.ci", link="l", code="1", locale="en")
    assert "connexion" in fr.subject.lower()
    assert fr.subject != en.subject


def test_unknown_locale_falls_back_rather_than_raising():
    msg = auth_svc.build_login_email("x@y.ci", link="l", code="1", locale="wolof")
    assert msg.subject
