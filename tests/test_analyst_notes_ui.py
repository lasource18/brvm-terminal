"""Phase 6c: Analyst view tab on /s/{ticker} + /s/{ticker}/analyst/{week}."""

from __future__ import annotations

from brvm.config import settings
from brvm.db import connect
from brvm.models import AnalystNote
from brvm.store import analyst_notes as notes_repo


def _seed(ticker: str = "SNTS", week: str = "2026-08-24",
          markdown: str = "# Snapshot\nHello.") -> None:
    with connect(settings.db_path) as conn:
        notes_repo.upsert(conn, AnalystNote(
            ticker=ticker, week_start=week, model="test",
            title="Snapshot", markdown=markdown, context_json="{}",
            input_tokens=1000, output_tokens=200, usd_micros=15_000,
            generated_utc="2026-08-24T20:00:00Z",
        ))


def test_analyst_tab_empty_state(client):
    r = client.get("/s/SNTS/analyst")
    assert r.status_code == 200
    body = r.text
    assert "No analyst note has been generated" in body
    assert "machine-generated when available" in body


def test_analyst_tab_in_tabbar_for_equities(client):
    r = client.get("/s/SNTS/chart")
    assert "Analyst view" in r.text
    assert 'href="/s/SNTS/analyst"' in r.text


def test_analyst_tab_hidden_for_indices(client):
    # BRVMC is seeded as an index in conftest — the tab must 404.
    r = client.get("/s/BRVMC/analyst")
    assert r.status_code == 404


def test_analyst_tab_renders_markdown_html(client):
    _seed()
    r = client.get("/s/SNTS/analyst")
    assert r.status_code == 200
    body = r.text
    # Markdown-it renders "# Snapshot" to <h1>.
    assert "<h1>Snapshot</h1>" in body
    assert "machine-generated" in body
    assert "2026-08-24" in body


def test_analyst_archive_route_serves_specific_week(client):
    _seed(week="2026-08-17", markdown="# Snapshot\nfoo")
    _seed(week="2026-08-24", markdown="# Snapshot\nbar")

    r = client.get("/s/SNTS/analyst/2026-08-17")
    assert r.status_code == 200
    # The main article renders the older note, not the latest.
    article = r.text.split("<article")[1].split("</article>")[0]
    assert "foo" in article
    assert "bar" not in article


def test_analyst_archive_unknown_week_404(client):
    r = client.get("/s/SNTS/analyst/2020-01-06")
    assert r.status_code == 404


def test_analyst_archive_bad_date_str_404(client):
    r = client.get("/s/SNTS/analyst/not-a-date")
    assert r.status_code == 404


def test_analyst_archive_indices_404(client):
    _seed(ticker="BRVMC", week="2026-08-24")  # store won't stop us seeding one
    r = client.get("/s/BRVMC/analyst/2026-08-24")
    assert r.status_code == 404


def test_analyst_archive_bonds_404(client, monkeypatch):
    """F-33: the archive route only blocked indices; the tab route
    hides analyst for bonds too. Bring them into alignment so a bond
    note (should one ever be seeded) doesn't leak through the
    archive URL."""
    from brvm.config import settings
    from brvm.db import connect
    from brvm.models import Security
    from brvm.store import securities as sec_repo
    with connect(settings.db_path) as conn:
        sec_repo.upsert(conn, [
            Security(ticker="BOND1", name="TEST BOND",
                     kind="bond", country="CI"),
        ])
    r = client.get("/s/BOND1/analyst/2026-08-24")
    assert r.status_code == 404
    assert "bond" in r.json()["detail"].lower()


def test_archive_sidebar_lists_prior_weeks(client):
    for w in ("2026-08-10", "2026-08-17", "2026-08-24"):
        _seed(week=w)
    r = client.get("/s/SNTS/analyst")
    body = r.text
    # Newest first.
    idx10 = body.index("2026-08-10")
    idx17 = body.index("2026-08-17")
    idx24 = body.index("2026-08-24")
    assert idx24 < idx17 < idx10
    # Current week is highlighted in the archive.
    assert 'class="current"' in body


def test_html_is_escaped_not_raw(client):
    _seed(markdown="<script>alert(1)</script>\n\nOK.")
    r = client.get("/s/SNTS/analyst")
    # markdown-it's html=False setting escapes raw HTML in the source.
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
