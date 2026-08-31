"""Production surface guards (PR-W).

Two things must not reach a user of the hosted product:

* the identity of the LLM behind the brief / analyst view, and
* what generating it cost.

Both were rendered in the page byline. The columns still exist in
`briefs` / `analyst_notes` — `store/spend.py` and the
`llm_daily_cap_cents` ceiling depend on them — so this is a display
boundary, not a schema change, and a template edit can silently undo it.
These tests seed a realistic model id and a non-trivial cost, then assert
neither is anywhere in the rendered HTML.
"""

from __future__ import annotations

from kodji.config import settings
from kodji.db import connect
from kodji.models import AnalystNote, Brief
from kodji.store import analyst_notes as notes_repo
from kodji.store import briefs as briefs_repo

# Deliberately realistic: a bare "test" model id would pass a substring
# assertion for the wrong reason.
_MODEL = "claude-sonnet-4-6"
_IN, _OUT, _USD_MICROS = 48_213, 3_907, 187_450  # $0.1875


def _seed_brief(day: str = "2026-08-20") -> None:
    with connect(settings.db_path) as conn:
        briefs_repo.upsert(conn, Brief(
            day=day, model=_MODEL, title="Session recap",
            markdown="# Session recap\nHello.", context_json="{}",
            input_tokens=_IN, output_tokens=_OUT, usd_micros=_USD_MICROS,
            generated_utc="2026-08-20T15:35:00Z", session_date=day,
        ))


def _seed_note(ticker: str = "SNTS", week: str = "2026-08-24") -> None:
    with connect(settings.db_path) as conn:
        notes_repo.upsert(conn, AnalystNote(
            ticker=ticker, week_start=week, model=_MODEL,
            title="Snapshot", markdown="# Snapshot\nHello.", context_json="{}",
            input_tokens=_IN, output_tokens=_OUT, usd_micros=_USD_MICROS,
            generated_utc="2026-08-24T20:00:00Z",
        ))


def _assert_clean(body: str, where: str) -> None:
    assert _MODEL not in body, f"{where}: leaks the model id"
    assert "claude" not in body.lower(), f"{where}: leaks a Claude reference"
    assert "anthropic" not in body.lower(), f"{where}: leaks the provider"
    assert str(_IN) not in body, f"{where}: leaks input token count"
    assert str(_OUT) not in body, f"{where}: leaks output token count"
    assert "0.1875" not in body, f"{where}: leaks the generation cost"
    assert "usd_micros" not in body, f"{where}: leaks the spend field"


def test_brief_page_hides_model_and_spend(client):
    _seed_brief()
    r = client.get("/brief")
    assert r.status_code == 200
    # The page still renders, and still discloses that it is machine output.
    assert "<h1>Session recap</h1>" in r.text
    assert "machine-generated" in r.text
    _assert_clean(r.text, "/brief")


def test_brief_archive_page_hides_model_and_spend(client):
    _seed_brief(day="2026-08-19")
    r = client.get("/brief/2026-08-19")
    assert r.status_code == 200
    _assert_clean(r.text, "/brief/2026-08-19")


def test_analyst_tab_hides_model_and_spend(client):
    _seed_note()
    r = client.get("/s/SNTS/analyst")
    assert r.status_code == 200
    assert "machine-generated" in r.text
    _assert_clean(r.text, "/s/SNTS/analyst")


def test_spend_accounting_still_works(client):
    """The display boundary must not have cost the cap its inputs."""
    _seed_brief()
    with connect(settings.db_path) as conn:
        row = briefs_repo.get(conn, "2026-08-20")
    assert row is not None
    assert row.model == _MODEL
    assert row.usd_micros == _USD_MICROS
    assert row.input_tokens == _IN


def test_tui_carries_no_subtitle():
    """The "Bloomberg-ish" subtitle is gone from the terminal chrome."""
    from kodji.apps.tui.app import KodjiTerminalApp

    assert not getattr(KodjiTerminalApp, "SUB_TITLE", "")
