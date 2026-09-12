"""Shared pytest fixtures."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def apply_migrations(conn) -> None:
    """Apply every migration in order. Tests use this instead of naming
    files so a new migration doesn't need a sweep through the suite.

    Records each id in `_schema_migrations` exactly as `scripts/
    migrate.py` does — otherwise a test DB looks unmigrated to
    `db.assert_schema_current`, which the web app runs at startup.
    """
    from kodji.db import ensure_migrations_table

    ensure_migrations_table(conn)
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.executescript(f.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT OR REPLACE INTO _schema_migrations(id, applied_utc) "
            "VALUES (?, datetime('now'))",
            (f.stem,),
        )
    conn.commit()


def reset_module_state() -> None:
    """Drop process-wide state that would leak across tests.

    Phase 6a replaced the old `importlib.reload` sweep with a lazy
    settings proxy (`config.reset_settings_cache`) — no module reloads
    needed because every `settings.X` reference is a proxy lookup, not
    a captured value. What's left is service-owned state (TTL caches,
    lazily-built LLM client) that a settings change or a fresh DB path
    should invalidate.
    """
    from kodji.config import reset_settings_cache

    reset_settings_cache()
    # TTL-cached scraper responses — a new DB path should not surface
    # data from the previous test's cache.
    try:
        from kodji.services import company as _company
        _company.clear_cache()
    except ImportError:
        pass
    try:
        from kodji.services import history as _history
        _history.clear_cache()
    except ImportError:
        pass
    # Memoized Anthropic SDK client — built against whatever
    # ANTHROPIC_API_KEY was in effect at first call.
    try:
        from kodji.services import llm as _llm
        _llm.reset_client()
    except ImportError:
        pass


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "kodji.sqlite"


def _seed(db_path: Path) -> None:
    from kodji.db import connect
    from kodji.models import IndexLevel, Quote, Security
    from kodji.store import quotes as quotes_repo
    from kodji.store import securities as sec_repo

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(
            conn,
            [
                Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
                Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
                Security(ticker="SPHC", name="SAPH CI", kind="equity", country="CI"),
                Security(ticker="CFAC", name="CFAO CI", kind="equity", country="CI"),
                Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
                Security(ticker="BRVM30", name="BRVM 30", kind="index"),
                Security(ticker="BRVMPR", name="BRVM - PRESTIGE", kind="index"),
            ],
        )
        quotes_repo.insert_snapshots(
            conn,
            [
                Quote(ticker="SNTS", source="sikafinance", last=32500, change_pct=1.88,
                      volume=3006, turnover=97_695_000),
                Quote(ticker="ORAC", source="sikafinance", last=19000, change_pct=-0.5,
                      volume=1000, turnover=19_000_000),
                Quote(ticker="SPHC", source="sikafinance", last=8990, change_pct=7.02,
                      volume=52971, turnover=476_209_290),
                Quote(ticker="CFAC", source="sikafinance", last=1600, change_pct=-3.90,
                      volume=4878, turnover=7_804_800),
            ],
        )
        quotes_repo.upsert_index_levels(
            conn,
            [
                IndexLevel(ticker="BRVMC", session_date=date(2026, 8, 19),
                           level=507.13, change_pct=1.16, source="sikafinance"),
            ],
        )


@pytest.fixture
def client(monkeypatch, tmp_path):
    """TestClient over a fresh, seeded SQLite DB with the APScheduler
    mocked, signed in as the operator on account 1 (paid)."""
    yield from _client(monkeypatch, tmp_path, sign_in=True)


@pytest.fixture
def anon_client(monkeypatch, tmp_path):
    """Same app and DB, but no operator user, session or cookie — for
    tests that count users/sessions or start from a blank slate."""
    yield from _client(monkeypatch, tmp_path, sign_in=False)


def _client(monkeypatch, tmp_path, *, sign_in: bool):
    from fastapi.testclient import TestClient

    db_path = tmp_path / "kodji.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    # Pin optional secrets to empty so the .env on a real developer's
    # machine doesn't leak into a test's rendered HTML (e.g. the /alerts
    # page's "no webhook" badge disappears if DISCORD_WEBHOOK_URL is set).
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    # Same reason, with teeth: a developer with a real RESEND_API_KEY in
    # .env would otherwise have the test suite send live email.
    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setenv("EMAIL_FROM", "")
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    # PR-Z: real Flutterwave keys in a developer's .env would open the
    # checkout buttons in rendered pages and make tests hit the API.
    monkeypatch.setenv("FLW_PUBLIC_KEY", "")
    monkeypatch.setenv("FLW_SECRET_KEY", "")
    monkeypatch.setenv("FLW_WEBHOOK_HASH", "")
    monkeypatch.setenv("BILLING_PROVIDER", "flutterwave")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "")
    monkeypatch.setenv("PAYSTACK_PUBLIC_KEY", "")
    # PR-AA: a developer's VAPID pair would flip the /alerts page from
    # "not configured" to live buttons. Tests that need push set their own.
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "")
    reset_module_state()
    _seed(db_path)

    from kodji.apps.web.main import app

    with patch("kodji.apps.web.main.build_scheduler") as bs:
        bs.return_value.get_jobs.return_value = []
        with TestClient(app) as c:
            if sign_in:
                _sign_in_as_operator(c, db_path)
            yield c

    reset_module_state()


OPERATOR_EMAIL = "owner@example.ci"


def _sign_in_as_operator(client, db_path: Path) -> None:
    """Give the client a session on account 1 — the operator's paid
    account from migration 0019.

    PR-Z made an anonymous web request the free tier no matter what
    account 1 holds, so the many tests that exercise paid pages need a
    real session, exactly like the operator has in production. Tests
    that want a signed-out visitor call `client.cookies.clear()`.
    """
    from datetime import timedelta

    from kodji.clock import utc_iso, utcnow
    from kodji.db import connect
    from kodji.store import accounts as accounts_repo
    from kodji.store import auth as auth_repo
    from kodji.store.accounts import DEFAULT_ACCOUNT_ID
    from kodji.store.auth import SESSION_COOKIE, hash_secret, new_token

    token = new_token()
    with connect(db_path) as conn:
        user_id, _ = accounts_repo.attach_user_to_account(conn, OPERATOR_EMAIL, DEFAULT_ACCOUNT_ID)
        auth_repo.create_session(
            conn,
            token_hash=hash_secret(token),
            user_id=user_id,
            account_id=DEFAULT_ACCOUNT_ID,
            expires_utc=utc_iso(utcnow() + timedelta(days=1)),
        )
    client.cookies.set(SESSION_COOKIE, token)
