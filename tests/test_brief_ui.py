"""Phase 6b: /brief page + /brief/YYYY-MM-DD archive."""

from __future__ import annotations

from brvm.config import settings
from brvm.db import connect
from brvm.models import Brief
from brvm.store import briefs as briefs_repo


def _seed(day: str = "2026-08-20", markdown: str = "# Session recap\nHello.") -> None:
    with connect(settings.db_path) as conn:
        briefs_repo.upsert(conn, Brief(
            day=day, model="test", title="Session recap",
            markdown=markdown, context_json="{}",
            input_tokens=100, output_tokens=50, usd_micros=1000,
            generated_utc="2026-08-20T15:35:00Z", session_date=day,
        ))


def test_brief_empty_state(client):
    r = client.get("/brief")
    assert r.status_code == 200
    body = r.text
    assert "No brief has been generated" in body
    # Archive sidebar renders empty.
    assert 'class="brief-archive"' not in body


def test_topbar_carries_brief_link(client):
    r = client.get("/")
    assert 'href="/brief"' in r.text


def test_brief_latest_renders_markdown_html(client):
    _seed()
    r = client.get("/brief")
    assert r.status_code == 200
    body = r.text
    # Markdown-it renders "# Session recap" to <h1>.
    assert "<h1>Session recap</h1>" in body
    assert "machine-generated" in body
    assert "2026-08-20" in body


def test_brief_by_day_route(client):
    _seed(day="2026-08-19", markdown="# Recap\nfoo")
    _seed(day="2026-08-20", markdown="# Recap\nbar")

    r = client.get("/brief/2026-08-19")
    assert r.status_code == 200
    assert "foo" in r.text
    assert "bar" not in r.text.split('<article')[1].split('</article>')[0]


def test_brief_unknown_date_404(client):
    r = client.get("/brief/2026-01-01")
    assert r.status_code == 404


def test_brief_bad_date_string_404(client):
    r = client.get("/brief/not-a-date")
    assert r.status_code == 404


def test_archive_sidebar_lists_recent_briefs(client):
    for d in ("2026-08-19", "2026-08-20", "2026-08-21"):
        _seed(day=d)
    r = client.get("/brief")
    body = r.text
    # Newest first.
    idx19 = body.index("2026-08-19")
    idx20 = body.index("2026-08-20")
    idx21 = body.index("2026-08-21")
    assert idx21 < idx20 < idx19
    # Latest brief is highlighted as current in the archive.
    assert 'class="current"' in body


def test_html_is_escaped_not_raw(client):
    _seed(markdown="<script>alert(1)</script>\n\nOK.")
    r = client.get("/brief")
    # markdown-it's html=False setting escapes raw HTML in the source.
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
