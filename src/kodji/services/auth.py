"""Magic-link sign-in.

The whole flow, in one place:

1. Someone submits an email address. We mint one challenge — a 256-bit
   URL token *and* a 6-digit code, both hashed into `login_tokens` — and
   mail both to that address.
2. They either open the link or type the code. Either path consumes the
   same challenge exactly once and mints a `sessions` row; the raw
   session token goes into a cookie and is never stored.
3. `services/accounts.current_account_id` turns that cookie back into an
   account id on every request.

There is no password, so there is nothing to leak, reset, or reuse from
another site's breach. What that trades away is that the mailbox *is* the
credential — which is why the challenge is short-lived, single-use,
rate-limited per address, and attempt-capped on the code.

Three decisions worth keeping
-----------------------------

**A GET never signs anyone in.** Mail scanners, link previewers and
Outlook Safe Links fetch every URL in a message before a human sees it.
If opening the link consumed the challenge, those prefetches would burn
it and the user would arrive at an expired link — a failure that looks
random and is nearly impossible to debug from a support email. So the
link renders a page with a button, and the POST behind it is what signs
in. `peek_token` is the read side of that split.

**The code is a first-class path, not a fallback.** A mail app that opens
the link in an in-app browser has its own cookie jar, so the session
lands somewhere the user cannot see. Typing six digits into the tab they
started in always works, including when they read mail on a different
device than the one they are browsing on.

**Enumeration is not defended here, and shouldn't be.** `request_login`
returns the same shape whether or not the address is known, and the route
renders the same page either way — but the *account* is only created when
a challenge is actually consumed, so an attacker who POSTs a thousand
addresses creates a thousand challenge rows and zero users.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from kodji.clock import utc_iso, utcnow
from kodji.config import settings
from kodji.db import connect
from kodji.i18n import DEFAULT_LOCALE
from kodji.logging import get
from kodji.services.mailer import EmailMessage, Mailer, get_mailer
from kodji.store import accounts as accounts_repo
from kodji.store import auth as auth_repo
from kodji.store.auth import SESSION_COOKIE, hash_secret, new_token

log = get(__name__)

__all__ = [
    "SESSION_COOKIE",
    "Grant",
    "LoginRequest",
    "build_login_email",
    "complete_with_code",
    "complete_with_token",
    "logout",
    "normalize_email",
    "peek_token",
    "purge_expired",
    "request_login",
]

# Deliberately loose. The only authority on whether an address exists is
# whether the mail arrives, so this rejects obvious typos and nothing
# else — a stricter pattern rejects valid addresses and buys nothing.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_MAX_EMAIL_LEN = 254  # RFC 5321 path limit


@dataclass(frozen=True)
class LoginRequest:
    """Outcome of asking for a link.

    `token` and `code` are the raw secrets. They are returned so tests and
    the no-key `ConsoleMailer` dev path can complete a sign-in; the web
    route never puts either in a response.
    """

    ok: bool
    email: str = ""
    note: str = ""
    token: str = ""
    code: str = ""
    expires_utc: str = ""


@dataclass(frozen=True)
class Grant:
    """A minted session: the raw cookie value plus who it belongs to."""

    token: str
    user_id: int
    account_id: int
    expires_utc: str


def _db_path() -> Path:
    return Path(settings.db_path)


def normalize_email(raw: str | None) -> str | None:
    """Trimmed, lowercased address — or None if it isn't one.

    Lowercasing at the edge keeps `users.email` consistent with the
    `lower(email)` unique index from migration 0017, so the address shown
    back to the user matches the one we matched on.
    """
    if not raw:
        return None
    email = raw.strip().lower()
    if len(email) > _MAX_EMAIL_LEN or not _EMAIL_RE.match(email):
        return None
    return email


def _new_code() -> str:
    """A 6-digit code. `secrets`, not `random` — this is a credential."""
    return f"{secrets.randbelow(1_000_000):06d}"


# --- step 1: ask for a link ------------------------------------------------


def request_login(
    raw_email: str,
    *,
    locale: str = DEFAULT_LOCALE,
    base_url: str = "",
    mailer: Mailer | None = None,
) -> LoginRequest:
    """Mint a challenge for `raw_email` and mail it.

    Callers pass a fake `mailer` to test the flow; production passes None
    and gets whatever `get_mailer()` resolves to.
    """
    email = normalize_email(raw_email)
    if email is None:
        return LoginRequest(ok=False, note="invalid_email")

    now = utcnow()
    window_start = utc_iso(now - timedelta(hours=1))
    expires_utc = utc_iso(now + timedelta(minutes=settings.login_token_ttl_minutes))

    token = new_token()
    code = _new_code()

    with connect(_db_path()) as conn:
        recent = auth_repo.count_recent_for_email(conn, email, window_start)
        if recent >= settings.login_max_per_hour:
            # Not surfaced differently to the user: the route renders the
            # same "check your mail" page, so this can't be used to probe
            # who has been asking for links.
            log.warning("login: rate limited %s (%d in the last hour)", email, recent)
            return LoginRequest(ok=False, email=email, note="rate_limited")

        auth_repo.create_login_token(
            conn,
            token_hash=hash_secret(token),
            code_hash=hash_secret(code),
            email=email,
            locale=locale,
            expires_utc=expires_utc,
        )

    link = f"{base_url.rstrip('/')}/login/t/{token}"
    msg = build_login_email(email, link=link, code=code, locale=locale)

    owns_mailer = mailer is None
    mailer = mailer or get_mailer()
    try:
        result = mailer.send(msg)
    finally:
        if owns_mailer:
            mailer.close()

    if not result.ok:
        log.error("login: send to %s failed: %s", email, result.note)
        # The challenge row stays. It expires on its own, and leaving it
        # keeps the rate limit honest against a provider that is failing
        # *after* accepting the message.
        return LoginRequest(ok=False, email=email, note="send_failed")

    log.info("login: challenge sent to %s (provider_id=%s)", email, result.provider_id)
    return LoginRequest(
        ok=True, email=email, token=token, code=code, expires_utc=expires_utc
    )


# --- step 2: prove it ------------------------------------------------------


def peek_token(raw_token: str) -> str | None:
    """The address a live link belongs to, without consuming it.

    This is what the GET behind a magic link calls. A prefetching scanner
    hitting it changes nothing.
    """
    if not raw_token:
        return None
    with connect(_db_path()) as conn:
        row = auth_repo.get_live_token(conn, hash_secret(raw_token), utc_iso())
        return str(row["email"]) if row else None


def complete_with_token(raw_token: str) -> Grant | None:
    """Consume a link and mint a session. None if it is spent or expired."""
    if not raw_token:
        return None
    token_hash = hash_secret(raw_token)
    with connect(_db_path()) as conn:
        row = auth_repo.get_live_token(conn, token_hash, utc_iso())
        if row is None:
            return None
        return _consume_and_grant(conn, token_hash, str(row["email"]))


def complete_with_code(raw_email: str, raw_code: str) -> Grant | None:
    """Consume the newest live challenge for an address by its code."""
    email = normalize_email(raw_email)
    code = (raw_code or "").strip()
    if email is None or not code:
        return None

    with connect(_db_path()) as conn:
        row = auth_repo.get_live_token_for_email(conn, email, utc_iso())
        if row is None:
            return None

        token_hash = str(row["token_hash"])
        if int(row["attempts"]) >= settings.login_code_max_attempts:
            # Already burned by earlier guesses; the row is only still
            # here because it hasn't expired yet.
            return None

        if not secrets.compare_digest(hash_secret(code), str(row["code_hash"])):
            attempts = auth_repo.bump_attempts(conn, token_hash)
            if attempts >= settings.login_code_max_attempts:
                # Burn the challenge rather than let it sit there for the
                # rest of its TTL absorbing guesses. The user asks for a
                # new one, which the per-address rate limit still bounds.
                auth_repo.consume_login_token(conn, token_hash, utc_iso())
                log.warning("login: challenge for %s burned after %d attempts",
                            email, attempts)
            return None

        return _consume_and_grant(conn, token_hash, email)


def _consume_and_grant(conn, token_hash: str, email: str) -> Grant | None:
    """Single-use consumption, then the session it earns.

    Consuming first is what makes this safe under concurrency: two
    requests carrying the same link both reach here, the UPDATE only
    matches for one of them, and the loser gets None instead of a second
    session.
    """
    if not auth_repo.consume_login_token(conn, token_hash, utc_iso()):
        return None

    # The account is created here, not at request time, so unconsumed
    # challenges never leave user rows behind.
    user_id, account_id = accounts_repo.ensure_user_with_account(conn, email)

    token = new_token()
    expires_utc = utc_iso(utcnow() + timedelta(days=settings.session_ttl_days))
    auth_repo.create_session(
        conn,
        token_hash=hash_secret(token),
        user_id=user_id,
        account_id=account_id,
        expires_utc=expires_utc,
    )
    log.info("login: session minted for %s (account=%d)", email, account_id)
    return Grant(
        token=token, user_id=user_id, account_id=account_id, expires_utc=expires_utc
    )


# --- session lifecycle -----------------------------------------------------


def logout(raw_token: str | None) -> bool:
    """Delete the session behind a cookie value. True if one was there."""
    if not raw_token:
        return False
    with connect(_db_path()) as conn:
        return auth_repo.delete_session(conn, hash_secret(raw_token)) > 0


def purge_expired() -> tuple[int, int]:
    """Housekeeping: drop expired sessions and spent challenges."""
    with connect(_db_path()) as conn:
        return auth_repo.purge_expired(conn, utc_iso())


# --- the message itself ----------------------------------------------------

_SUBJECT = {
    "fr": "Votre lien de connexion Kodji",
    "en": "Your Kodji sign-in link",
}

_TEXT = {
    "fr": (
        "Bonjour,\n\n"
        "Voici votre lien de connexion à Kodji Terminal :\n\n"
        "{link}\n\n"
        "Ou saisissez ce code dans l'onglet où vous avez demandé la connexion :\n\n"
        "    {code}\n\n"
        "Le lien et le code expirent dans {ttl} minutes et ne servent qu'une fois.\n"
        "Si vous n'avez rien demandé, ignorez ce message — aucun compte n'a été créé.\n"
    ),
    "en": (
        "Hello,\n\n"
        "Here is your sign-in link for Kodji Terminal:\n\n"
        "{link}\n\n"
        "Or enter this code in the tab where you asked to sign in:\n\n"
        "    {code}\n\n"
        "The link and code expire in {ttl} minutes and work only once.\n"
        "If you didn't request this, ignore this message — no account was created.\n"
    ),
}

_HTML = {
    "fr": (
        "<p>Bonjour,</p>"
        '<p><a href="{link}">Se connecter à Kodji Terminal</a></p>'
        "<p>Ou saisissez ce code dans l'onglet où vous avez demandé la "
        "connexion :</p>"
        '<p style="font-size:22px;letter-spacing:4px;'
        'font-family:monospace"><strong>{code}</strong></p>'
        "<p>Le lien et le code expirent dans {ttl} minutes et ne servent "
        "qu'une fois. Si vous n'avez rien demandé, ignorez ce message — "
        "aucun compte n'a été créé.</p>"
    ),
    "en": (
        "<p>Hello,</p>"
        '<p><a href="{link}">Sign in to Kodji Terminal</a></p>'
        "<p>Or enter this code in the tab where you asked to sign in:</p>"
        '<p style="font-size:22px;letter-spacing:4px;'
        'font-family:monospace"><strong>{code}</strong></p>'
        "<p>The link and code expire in {ttl} minutes and work only once. "
        "If you didn't request this, ignore this message — no account was "
        "created.</p>"
    ),
}


def build_login_email(
    email: str, *, link: str, code: str, locale: str = DEFAULT_LOCALE
) -> EmailMessage:
    """The sign-in message, in the language the browser was in.

    Written in FR/EN here rather than through the `t` filter because
    `i18n` is a UI-chrome catalog keyed on English source strings, and
    these are multi-paragraph bodies with placeholders. Kept plain: no
    images, no tracking pixel, no CSS beyond one inline rule. That is
    partly taste and partly deliverability — a text-heavy transactional
    mail with a matching plain-text part is what filters expect.
    """
    lang = locale if locale in _SUBJECT else DEFAULT_LOCALE
    ttl = settings.login_token_ttl_minutes
    return EmailMessage(
        to=email,
        subject=_SUBJECT[lang],
        text=_TEXT[lang].format(link=link, code=code, ttl=ttl),
        html=_HTML[lang].format(link=link, code=code, ttl=ttl),
    )
