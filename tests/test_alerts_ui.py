"""Phase 6a: /alerts page + fragment endpoints."""

from __future__ import annotations


def test_alerts_page_empty_state(client):
    r = client.get("/alerts")
    assert r.status_code == 200
    body = r.text
    assert "Alerts" in body
    assert "No rules yet" in body
    assert "no webhook" in body  # DISCORD_WEBHOOK_URL is unset in tests


def test_topbar_carries_alerts_link(client):
    r = client.get("/")
    assert 'href="/alerts"' in r.text


def test_create_rule_via_fragment_appears_in_the_list(client):
    r = client.post(
        "/_frag/alerts/rules",
        data={
            "kind": "price_move",
            "ticker": "snts",   # lowercased on the wire, upper-cased in the handler
            "label": "big moves",
            "threshold_pct": "3.5",
        },
    )
    assert r.status_code == 200
    assert "SNTS" in r.text
    assert "big moves" in r.text
    assert "|Δ| ≥ 3.5%" in r.text


def test_create_price_move_rule_needs_threshold(client):
    r = client.post(
        "/_frag/alerts/rules",
        data={"kind": "price_move", "ticker": "SNTS"},
    )
    assert r.status_code == 400


def test_create_news_rule_with_relevance(client):
    r = client.post(
        "/_frag/alerts/rules",
        data={"kind": "news", "min_relevance": "8"},
    )
    assert r.status_code == 200
    assert "relevance ≥ 8" in r.text


def test_toggle_rule_flips_enabled(client):
    r = client.post(
        "/_frag/alerts/rules",
        data={"kind": "price_move", "threshold_pct": "5.0"},
    )
    assert r.status_code == 200

    # Rule id is the only one in the table.
    from kodji.config import settings
    from kodji.db import connect
    from kodji.store import alerts as alerts_repo

    with connect(settings.db_path) as conn:
        rules = alerts_repo.list_rules(conn)
    assert len(rules) == 1
    rid = rules[0].id

    r2 = client.post(f"/_frag/alerts/rules/{rid}/toggle")
    assert r2.status_code == 200
    with connect(settings.db_path) as conn:
        assert alerts_repo.get_rule(conn, rid).enabled is False


def test_delete_rule_removes_it(client):
    client.post(
        "/_frag/alerts/rules",
        data={"kind": "news", "min_relevance": "5"},
    )
    from kodji.config import settings
    from kodji.db import connect
    from kodji.store import alerts as alerts_repo

    with connect(settings.db_path) as conn:
        rid = alerts_repo.list_rules(conn)[0].id

    r2 = client.delete(f"/_frag/alerts/rules/{rid}")
    assert r2.status_code == 200
    assert "No rules yet" in r2.text


def test_delete_missing_rule_returns_404(client):
    r = client.delete("/_frag/alerts/rules/9999")
    assert r.status_code == 404


def test_toggle_missing_rule_returns_404(client):
    r = client.post("/_frag/alerts/rules/9999/toggle")
    assert r.status_code == 404


def test_create_rule_with_unknown_kind_rejected(client):
    r = client.post(
        "/_frag/alerts/rules",
        data={"kind": "bogus"},
    )
    assert r.status_code == 400


def test_create_price_move_rule_rejects_non_numeric_threshold(client):
    """F-21: `float('abc')` used to bubble a 500 through the fragment.
    Non-numeric input is user error and should return 400."""
    r = client.post(
        "/_frag/alerts/rules",
        data={"kind": "price_move", "threshold_pct": "abc"},
    )
    assert r.status_code == 400
    assert "numeric" in r.text.lower()


def test_create_price_move_rule_rejects_zero_threshold(client):
    """F-21: a 0 threshold matches every move (|change| >= 0 is
    always true), turning any price_move rule into a fire-on-
    everything trap. Require > 0."""
    r = client.post(
        "/_frag/alerts/rules",
        data={"kind": "price_move", "threshold_pct": "0"},
    )
    assert r.status_code == 400


def test_create_news_rule_rejects_non_numeric_relevance(client):
    """Same as F-21 for the news rule's min_relevance field."""
    r = client.post(
        "/_frag/alerts/rules",
        data={"kind": "news", "min_relevance": "very"},
    )
    assert r.status_code == 400
