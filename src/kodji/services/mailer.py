"""Outbound email — the transport, not the content.

One `Mailer` protocol with two implementations: `ResendMailer` for
production and `ConsoleMailer` for a machine with no API key, which logs
the message instead of dropping it. `get_mailer()` picks between them, so
a fresh clone can complete a magic-link sign-in from the log line without
anyone signing up for anything.

**Why Resend** (decided 2026-08-31). Deliverability to Abidjan, Dakar or
Ouagadougou is decided by the recipient's mailbox provider — overwhelmingly
Gmail, Yahoo and Outlook, on global anycast — and by our own domain
authentication, not by which continent the inbox is read on. So the choice
came down to cost at our volume and time-to-first-email: Resend's free tier
covers the whole pre-revenue period, and it sends from Ireland, the closest
region to West Africa by network path. Postmark is the escalation if
sign-in mail ever starts landing in spam; SES only makes sense past a
volume we are nowhere near.

**Keep the daily brief off this sender.** A brief blast is bulk-shaped and
attracts spam complaints; sign-in mail must not share its reputation. When
the brief goes out by email it gets its own subdomain and its own key.

**WhatsApp lands here as a sibling, not a rewrite.** `SendResult` is
already channel-neutral and `services/auth.py` calls a single `send()`;
a WhatsApp channel implements the same two methods against a verified
phone number, and the auth flow picks a channel per user instead of
assuming email. Nothing above this module needs to know which one ran.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Protocol

import httpx

from kodji.config import settings
from kodji.logging import get

log = get(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str


@dataclass(frozen=True)
class SendResult:
    """Outcome of one send.

    `permanent` mirrors the alert-delivery convention: a 4xx that will
    never succeed (bad address, revoked key) is permanent, while a 5xx,
    a 429 or a timeout is worth retrying. Callers use it to decide
    between "tell the user it failed" and "try again".
    """

    ok: bool
    note: str = ""
    permanent: bool = False
    provider_id: str = ""


class Mailer(Protocol):
    def send(self, msg: EmailMessage) -> SendResult: ...

    def close(self) -> None: ...


class ResendMailer:
    """Resend's REST API. One POST per message.

    Sends `text` alongside `html` always: a multipart message is both
    better-scoring with spam filters and the only thing some low-bandwidth
    mobile clients render.
    """

    def __init__(
        self,
        api_key: str,
        sender: str,
        *,
        client: httpx.Client | None = None,
        timeout: float | None = None,
    ) -> None:
        self._api_key = api_key
        self._sender = sender
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout or settings.http_timeout_s
        )

    def payload(self, msg: EmailMessage) -> dict:
        """The request body — exposed so tests can pin the wire format."""
        return {
            "from": self._sender,
            "to": [msg.to],
            "subject": msg.subject,
            "text": msg.text,
            "html": msg.html,
        }

    def send(self, msg: EmailMessage) -> SendResult:
        try:
            resp = self._client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=self.payload(msg),
            )
        except httpx.HTTPError as e:
            return SendResult(ok=False, note=f"transport: {e}")

        if resp.status_code < 300:
            provider_id = ""
            # The id is for log correlation only, so a body we can't parse
            # must not turn a delivered message into a failed one.
            with contextlib.suppress(ValueError):
                provider_id = str(resp.json().get("id") or "")
            return SendResult(ok=True, provider_id=provider_id)

        # 429 is a rate limit, not a rejection — retryable like a 5xx.
        permanent = 400 <= resp.status_code < 500 and resp.status_code != 429
        note = f"http {resp.status_code}: {resp.text[:200]}"
        return SendResult(ok=False, note=note, permanent=permanent)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class ConsoleMailer:
    """Logs the message instead of sending it.

    What runs when `RESEND_API_KEY` is unset. `sent` keeps the messages
    so the TUI, a test, or a developer can read back what would have
    gone out; it is bounded because a long dev session that never
    restarts should not accumulate messages forever.
    """

    MAX_KEPT = 50

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, msg: EmailMessage) -> SendResult:
        self.sent.append(msg)
        del self.sent[: -self.MAX_KEPT]
        log.warning(
            "email not sent (no RESEND_API_KEY) — to=%s subject=%s\n%s",
            msg.to,
            msg.subject,
            msg.text,
        )
        return SendResult(ok=True, note="console")

    def close(self) -> None:
        return None


def get_mailer() -> Mailer:
    """The mailer for the current configuration.

    Both settings are required for a real send: a key without a verified
    `EMAIL_FROM` produces a 422 from Resend on every message, which is a
    worse failure than falling back to the log.
    """
    if settings.has_email:
        return ResendMailer(settings.resend_api_key, settings.email_from)
    return ConsoleMailer()
