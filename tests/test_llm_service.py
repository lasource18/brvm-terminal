"""Tests for services/llm.py: prompt shape, validation, retry, cost."""

from __future__ import annotations

import json

import pytest

from brvm.services import llm

from ._fake_anthropic import FakeAnthropic, FakeResponse, json_reply, reply, tag_for

UNIVERSE = [
    ("SNTS", "SONATEL", "TELECOMMUNICATIONS"),
    ("ORAC", "ORANGE COTE D'IVOIRE", "TELECOMMUNICATIONS"),
    ("BRVMC", "BRVM COMPOSITE", None),
]

ITEMS = [
    llm.TagItem(
        id=1,
        title="SONATEL : résultats du 1er semestre 2026",
        kind="communique",
        source="sikafinance",
        issuer_name="SONATEL",
        ticker_hint="SNTS",
        published_at="2026-08-20T09:00:00Z",
    ),
    llm.TagItem(
        id=2,
        title="La BCEAO maintient son taux directeur",
        chapeau="Le comité de politique monétaire s'est réuni à Dakar.",
        source="sikafinance",
    ),
]


# --- prompt ---------------------------------------------------------------


def test_system_prompt_carries_the_live_universe():
    prompt = llm.build_system_prompt(UNIVERSE)
    assert "SNTS\tSONATEL\tTELECOMMUNICATIONS" in prompt
    assert "BRVMC\tBRVM COMPOSITE" in prompt  # no sector -> no trailing tab field
    for cat in llm.CATEGORIES:
        assert cat in prompt


def test_user_payload_is_json_and_drops_empty_fields():
    payload = llm.build_user_payload(ITEMS)
    data = json.loads(payload.split("INPUT:\n", 1)[1])
    assert [d["id"] for d in data] == [1, 2]
    assert data[0]["ticker_hint"] == "SNTS"
    assert "chapeau" not in data[0]  # None is omitted, not sent as null
    assert "ticker_hint" not in data[1]


# --- pricing --------------------------------------------------------------


def test_price_lookup_matches_dated_snapshot():
    assert llm.price_per_mtok("claude-haiku-4-5-20251001") == (1.00, 5.00)
    assert llm.price_per_mtok("claude-opus-5") == (5.00, 25.00)


def test_price_lookup_falls_back_for_unknown_model():
    assert llm.price_per_mtok("claude-something-new") == (1.00, 5.00)


def test_cost_is_tracked_below_one_cent():
    # 1000 in + 400 out on Haiku = $0.001 + $0.002 = $0.003 = 3000 micros.
    micros = llm.usd_micros_for("claude-haiku-4-5-20251001", input_tokens=1000, output_tokens=400)
    assert micros == 3000
    # ...which would have rounded to 0 in whole cents. That's why we store micros.
    assert round(micros / 10_000) == 0


def test_cost_applies_cache_multipliers():
    micros = llm.usd_micros_for(
        "claude-haiku-4-5-20251001",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=10_000,  # 0.1x -> $0.001
        cache_write_tokens=10_000,  # 1.25x -> $0.0125
    )
    assert micros == 1000 + 12_500


# --- the call ------------------------------------------------------------


def test_tag_batch_happy_path():
    client = FakeAnthropic([json_reply([tag_for(1), tag_for(2, tickers=[], category="macro")])])
    result = llm.tag_batch(ITEMS, UNIVERSE, client=client, model="claude-haiku-4-5-20251001")

    assert [t.id for t in result.tags] == [1, 2]
    assert result.tags[0].tickers == ["SNTS"]
    assert result.tags[1].category == "macro"
    assert result.attempts == 1
    assert result.usage.calls == 1
    assert result.usage.usd_micros == 3000

    sent = client.calls[0]
    assert sent["model"] == "claude-haiku-4-5-20251001"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    # System prompt is the stable prefix, so it carries the cache breakpoint.
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_tag_batch_drops_tickers_outside_the_universe():
    client = FakeAnthropic([json_reply([tag_for(1, tickers=["SNTS", "AAPL", "snts"])])])
    result = llm.tag_batch(ITEMS[:1], UNIVERSE, client=client)
    assert result.tags[0].tickers == ["SNTS"]  # unknown dropped, dupe collapsed


def test_tag_batch_drops_hallucinated_ids():
    client = FakeAnthropic([json_reply([tag_for(1), tag_for(999)])])
    result = llm.tag_batch(ITEMS, UNIVERSE, client=client)
    assert [t.id for t in result.tags] == [1]


def test_tag_batch_clamps_relevance():
    client = FakeAnthropic([json_reply([tag_for(1, relevance=99)])])
    result = llm.tag_batch(ITEMS[:1], UNIVERSE, client=client)
    assert result.tags[0].relevance == 10


def test_tag_batch_retries_once_on_unparseable_reply():
    client = FakeAnthropic([reply("Voici les résultats :"), json_reply([tag_for(1)])])
    result = llm.tag_batch(ITEMS[:1], UNIVERSE, client=client)

    assert result.attempts == 2
    assert result.usage.calls == 2  # both attempts were billed
    # The retry feeds the bad reply back so the model can correct itself.
    retry_messages = client.calls[1]["messages"]
    assert [m["role"] for m in retry_messages] == ["user", "assistant", "user"]
    assert "could not be used" in retry_messages[-1]["content"]


def test_tag_batch_gives_up_after_max_attempts_but_reports_usage():
    client = FakeAnthropic([reply("nope"), reply("still nope")])
    with pytest.raises(llm.LLMResponseError) as exc:
        llm.tag_batch(ITEMS[:1], UNIVERSE, client=client)
    assert exc.value.usage.calls == 2
    assert exc.value.usage.usd_micros == 6000


def test_tag_batch_does_not_retry_a_truncated_reply():
    truncated = json_reply([tag_for(1)])
    truncated.stop_reason = "max_tokens"
    client = FakeAnthropic([truncated])
    with pytest.raises(llm.LLMResponseError, match="truncated"):
        llm.tag_batch(ITEMS, UNIVERSE, client=client)
    assert client.call_count == 1  # a verbatim retry would truncate identically


def test_tag_batch_surfaces_a_refusal():
    refused = FakeResponse(content=[], stop_reason="refusal")
    client = FakeAnthropic([refused])
    with pytest.raises(llm.LLMResponseError, match="refused"):
        llm.tag_batch(ITEMS, UNIVERSE, client=client)


def test_tag_batch_on_empty_input_makes_no_call():
    client = FakeAnthropic([])
    result = llm.tag_batch([], UNIVERSE, client=client)
    assert result.tags == [] and client.call_count == 0


def test_get_client_without_key_raises_llm_unavailable(monkeypatch):
    from brvm.config import reset_settings_cache

    llm.reset_client()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    reset_settings_cache()
    with pytest.raises(llm.LLMUnavailable, match="ANTHROPIC_API_KEY"):
        llm.get_client()
