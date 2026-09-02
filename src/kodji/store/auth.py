"""SQLite repository for sign-in challenges and sessions.

Two tables, both keyed on a SHA-256 digest rather than the secret itself:
`login_tokens` (migration 0018) is the short-lived challenge sent by
email, `sessions` (migration 0017) is what a consumed challenge mints.

The hashing is the point. `token_hash` is a primary key, so a lookup is
an index hit on the digest and the plaintext token never touches the
database — a stolen SQLite file yields no usable link and no live
session. Comparisons go through `secrets.compare_digest`, which is why
the raw value is hashed at the edge and compared here rather than being
selected out and `==`-ed by a caller.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3

from kodji.clock import utc_iso

# Session cookie is set and read in the web layer; the name lives with
# the session table so both ends of it move together.
SESSION_COOKIE = "kodji_session"


def hash_secret(raw: str) -> str:
    """SHA-256 hex digest of a token or code.

    Plain SHA-256, not a password KDF, deliberately: these are 256-bit
    random values with a 20-minute lifetime, not user-chosen secrets, so
    there is no dictionary to slow an attacker down against. The 6-digit
    code is the exception — it is guessable by construction, which is
    what `attempts` and the rate limit exist to bound.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_token() -> str:
    """A URL-safe 256-bit secret for a magic link or a session cookie."""
    return secrets.token_urlsafe(32)


# --- login challenges ------------------------------------------------------


def create_login_token(
    conn: sqlite3.Connection,
    *,
    token_hash: str,
    code_hash: str,
    email: str,
    locale: str,
    expires_utc: str,
) -> None:
    conn.execute(
        """
        INSERT INTO login_tokens
            (token_hash, code_hash, email, locale, created_utc, expires_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (token_hash, code_hash, email.strip(), locale, utc_iso(), expires_utc),
    )
    conn.commit()


def get_live_token(
    conn: sqlite3.Connection, token_hash: str, now_utc: str
) -> sqlite3.Row | None:
    """The challenge for this digest, only while it is still usable.

    Expiry and single-use are enforced in SQL rather than by the caller,
    so there is no window where a service forgets to check one of them.
    """
    return conn.execute(
        """
        SELECT * FROM login_tokens
        WHERE token_hash = ? AND consumed_utc IS NULL AND expires_utc > ?
        """,
        (token_hash, now_utc),
    ).fetchone()


def get_live_token_for_email(
    conn: sqlite3.Connection, email: str, now_utc: str
) -> sqlite3.Row | None:
    """Newest live challenge for an address — the code-entry path.

    Newest wins: a user who asks for a second link because the first was
    slow to arrive should be able to type the code from either mail, but
    only the most recent one is honoured, so an older mail sitting in an
    inbox stops being a credential the moment a new one is requested.
    """
    return conn.execute(
        """
        SELECT * FROM login_tokens
        WHERE lower(email) = lower(?)
          AND consumed_utc IS NULL
          AND expires_utc > ?
        ORDER BY created_utc DESC, rowid DESC
        LIMIT 1
        """,
        (email.strip(), now_utc),
    ).fetchone()


def consume_login_token(conn: sqlite3.Connection, token_hash: str, now_utc: str) -> bool:
    """Mark a challenge used. Returns False if it already was.

    The `consumed_utc IS NULL` in the WHERE clause makes this the atomic
    check-and-set that single-use rests on: two concurrent requests for
    the same link both reach here, and exactly one sees `rowcount == 1`.
    """
    cur = conn.execute(
        "UPDATE login_tokens SET consumed_utc = ? "
        "WHERE token_hash = ? AND consumed_utc IS NULL",
        (now_utc, token_hash),
    )
    conn.commit()
    return cur.rowcount == 1


def bump_attempts(conn: sqlite3.Connection, token_hash: str) -> int:
    """Record a wrong code guess; returns the new attempt count."""
    conn.execute(
        "UPDATE login_tokens SET attempts = attempts + 1 WHERE token_hash = ?",
        (token_hash,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT attempts FROM login_tokens WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    return int(row["attempts"]) if row else 0


def count_recent_for_email(
    conn: sqlite3.Connection, email: str, since_utc: str
) -> int:
    """Challenges minted for this address since `since_utc` — rate limit input."""
    row = conn.execute(
        "SELECT count(*) AS n FROM login_tokens "
        "WHERE lower(email) = lower(?) AND created_utc >= ?",
        (email.strip(), since_utc),
    ).fetchone()
    return int(row["n"]) if row else 0


# --- sessions --------------------------------------------------------------


def create_session(
    conn: sqlite3.Connection,
    *,
    token_hash: str,
    user_id: int,
    account_id: int,
    expires_utc: str,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions (token_hash, user_id, account_id, created_utc, expires_utc)
        VALUES (?, ?, ?, ?, ?)
        """,
        (token_hash, user_id, account_id, utc_iso(), expires_utc),
    )
    conn.commit()


def get_active_session(
    conn: sqlite3.Connection, token_hash: str, now_utc: str
) -> sqlite3.Row | None:
    """The session for this cookie digest, only while unexpired.

    Expired rows are left for `purge_expired` rather than deleted here:
    reading a session happens on every request, and a read path that
    writes turns every page view into a WAL commit.
    """
    return conn.execute(
        "SELECT * FROM sessions WHERE token_hash = ? AND expires_utc > ?",
        (token_hash, now_utc),
    ).fetchone()


def get_active_session_with_user(
    conn: sqlite3.Connection, token_hash: str, now_utc: str
) -> sqlite3.Row | None:
    """`get_active_session` plus the member's email, in one round trip.

    This runs on every full-page render, so it is one indexed lookup and
    a join rather than two statements.
    """
    return conn.execute(
        """
        SELECT s.user_id, s.account_id, u.email
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ? AND s.expires_utc > ?
        """,
        (token_hash, now_utc),
    ).fetchone()


def delete_session(conn: sqlite3.Connection, token_hash: str) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
    conn.commit()
    return cur.rowcount


def purge_expired(conn: sqlite3.Connection, now_utc: str) -> tuple[int, int]:
    """Drop expired sessions and spent/expired challenges.

    Returns `(sessions, login_tokens)` removed. Consumed challenges are
    dropped too — once used they are dead weight, and keeping them would
    make `login_tokens` grow without bound on a long-lived install.
    """
    sessions = conn.execute(
        "DELETE FROM sessions WHERE expires_utc <= ?", (now_utc,)
    ).rowcount
    tokens = conn.execute(
        "DELETE FROM login_tokens WHERE expires_utc <= ? OR consumed_utc IS NOT NULL",
        (now_utc,),
    ).rowcount
    conn.commit()
    return sessions, tokens
