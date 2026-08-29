"""Tests for services/extraction.py: PDF read, preflight, prompt, retry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brvm.services import extraction

from ._fake_anthropic import FakeAnthropic, FakeResponse, reply

# --- extract_pdf_text -----------------------------------------------------


def test_extract_pdf_text_flags_a_scanned_pdf():
    """The tiny 2-page fixture has no extractable text, mimicking a scanned
    report. The extractor must flag it and the caller will short-circuit."""
    pdf_path = Path("tests/fixtures/brvm_org/tiny_two_page.pdf")
    parsed = extraction.extract_pdf_text(pdf_path)
    assert parsed.is_scanned is True
    assert parsed.text == ""
    assert parsed.page_count >= 1


# --- preflight ------------------------------------------------------------


def test_preflight_estimates_scale_with_input_size():
    small = extraction.preflight("x" * 100)
    big = extraction.preflight("x" * 400_000)
    assert big.est_usd_micros > small.est_usd_micros
    assert big.est_input_tokens > small.est_input_tokens


def test_preflight_reports_truncation_at_the_hard_cap():
    est = extraction.preflight("x" * (extraction._MAX_TEXT_CHARS + 10))
    assert est.truncated is True


# --- prompt shape ---------------------------------------------------------


def test_user_payload_carries_metadata_and_body():
    payload = extraction.build_user_payload(
        ticker="SNTS",
        issuer_name="SONATEL",
        period_year_hint=2024,
        period_kind_hint="annual",
        pdf_text="Chiffre d'affaires: 1 500 000 000 FCFA",
    )
    header, _, body = payload.partition("FILING TEXT:\n")
    assert "\"ticker\": \"SNTS\"" in header
    assert "\"period_year_hint\": 2024" in header
    assert body.startswith("Chiffre d'affaires")


def test_user_payload_drops_none_metadata():
    payload = extraction.build_user_payload(
        ticker="SNTS",
        issuer_name=None,
        period_year_hint=None,
        period_kind_hint=None,
        pdf_text="some text",
    )
    header = payload.split("FILING TEXT:", 1)[0]
    assert "issuer_name" not in header
    assert "period_year_hint" not in header


# --- extract_filing (the actual API call) ---------------------------------


def _json_reply(data: dict, **usage: int) -> FakeResponse:
    return reply(json.dumps(data, ensure_ascii=False), **usage)


def _happy_extract(**over) -> dict:
    base = {
        "period_year": 2024,
        "period_kind": "annual",
        "currency": "XOF",
        "revenue": 1_500_000_000,
        "operating_income": 400_000_000,
        "net_income": 300_000_000,
        "total_assets": 5_000_000_000,
        "total_equity": 2_000_000_000,
        "eps": 1_200.50,
        "dividend_per_share": 500.0,
        "segments": [
            {"name": "Sénégal", "segment_kind": "geo", "revenue": 900_000_000, "share_pct": 60.0},
            {"name": "Mali", "segment_kind": "geo", "revenue": 600_000_000, "share_pct": 40.0},
        ],
        "ownership": [
            {"holder": "SONATEL SA", "share_pct": 42.3, "shares": 4_230_000},
            {"holder": "Flottant", "share_pct": 57.7, "shares": None},
        ],
    }
    base.update(over)
    return base


def test_extract_filing_happy_path():
    client = FakeAnthropic([_json_reply(_happy_extract())])
    result = extraction.extract_filing(
        ticker="SNTS",
        issuer_name="SONATEL",
        pdf_text="Chiffre d'affaires: 1 500 000 000 FCFA",
        period_year_hint=2024,
        client=client,
        model="claude-haiku-4-5-20251001",
    )
    assert result.extract.period_year == 2024
    assert result.extract.revenue == 1_500_000_000
    assert len(result.extract.segments) == 2
    assert len(result.extract.ownership) == 2
    assert result.usage.calls == 1

    sent = client.calls[0]
    assert sent["model"] == "claude-haiku-4-5-20251001"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    # System prompt stays cached: same instructions on every filing.
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_extract_filing_refuses_empty_text():
    with pytest.raises(ValueError, match="empty PDF text"):
        extraction.extract_filing(
            ticker="SNTS", issuer_name=None, pdf_text="   ", client=FakeAnthropic([])
        )


def test_extract_filing_clamps_out_of_range_share_pct():
    data = _happy_extract(
        segments=[{"name": "X", "segment_kind": "business", "share_pct": 150.0}],
        ownership=[{"holder": "Y", "share_pct": -3.0}],
    )
    client = FakeAnthropic([_json_reply(data)])
    result = extraction.extract_filing(
        ticker="SNTS", issuer_name=None, pdf_text="text", client=client
    )
    assert result.extract.segments[0].share_pct == 100.0
    assert result.extract.ownership[0].share_pct == 0.0


def test_extract_filing_drops_impossible_period_year():
    data = _happy_extract(period_year=1800)
    client = FakeAnthropic([_json_reply(data)])
    result = extraction.extract_filing(
        ticker="SNTS", issuer_name=None, pdf_text="text", client=client
    )
    assert result.extract.period_year is None


def test_extract_filing_drops_negative_totals():
    data = _happy_extract(revenue=-100, total_equity=-50)
    client = FakeAnthropic([_json_reply(data)])
    result = extraction.extract_filing(
        ticker="SNTS", issuer_name=None, pdf_text="text", client=client
    )
    assert result.extract.revenue is None
    assert result.extract.total_equity is None


def test_extract_filing_retries_once_on_unparseable():
    client = FakeAnthropic([reply("prose not json"), _json_reply(_happy_extract())])
    result = extraction.extract_filing(
        ticker="SNTS", issuer_name=None, pdf_text="text", client=client
    )
    assert result.attempts == 2
    assert result.usage.calls == 2  # both attempts billed
    # Feedback message contains the corrective prompt.
    assert "could not be used" in client.calls[1]["messages"][-1]["content"]


def test_extract_filing_gives_up_after_max_attempts_but_reports_usage():
    client = FakeAnthropic([reply("nope"), reply("still nope")])
    with pytest.raises(extraction.LLMResponseError) as exc:
        extraction.extract_filing(
            ticker="SNTS", issuer_name=None, pdf_text="text", client=client
        )
    assert exc.value.usage.calls == 2


def test_extract_filing_does_not_retry_truncation():
    truncated = _json_reply(_happy_extract())
    truncated.stop_reason = "max_tokens"
    client = FakeAnthropic([truncated])
    with pytest.raises(extraction.LLMResponseError, match="truncated"):
        extraction.extract_filing(
            ticker="SNTS", issuer_name=None, pdf_text="text", client=client
        )
    assert client.call_count == 1


def test_extract_filing_preserves_billed_usage_on_transport_retry_error():
    """F-22 mirror for the extractor: attempt 1 billed but unusable,
    attempt 2 raises a transport error. The `LLMResponseError` carries
    attempt 1's usage so the caller's spend recording lands correctly."""
    client = FakeAnthropic([
        reply("bogus"),
        RuntimeError("connection reset"),
    ])
    with pytest.raises(extraction.LLMResponseError) as exc:
        extraction.extract_filing(
            ticker="SNTS", issuer_name=None, pdf_text="text", client=client
        )
    assert exc.value.usage.calls == 1
    assert "transport error" in str(exc.value)


def test_extract_filing_surfaces_refusal():
    refused = FakeResponse(content=[], stop_reason="refusal")
    client = FakeAnthropic([refused])
    with pytest.raises(extraction.LLMResponseError, match="refused"):
        extraction.extract_filing(
            ticker="SNTS", issuer_name=None, pdf_text="text", client=client
        )


# --- cash-flow extraction (Phase 7) --------------------------------------


def test_extract_filing_populates_cash_flow_fields_from_reply():
    data = _happy_extract(
        cash_flow_ops=500_000_000,
        capex=200_000_000,
        free_cash_flow=300_000_000,
    )
    client = FakeAnthropic([_json_reply(data)])
    result = extraction.extract_filing(
        ticker="SNTS", issuer_name=None, pdf_text="text", client=client
    )
    assert result.extract.cash_flow_ops == 500_000_000
    assert result.extract.capex == 200_000_000
    assert result.extract.free_cash_flow == 300_000_000


def test_extract_filing_flips_negative_capex_to_positive():
    """French reports show capex as an outflow ("-200 M"). We store it
    positive so FCF = CFO - capex has the conventional sign regardless
    of how the model transcribed the source."""
    data = _happy_extract(
        cash_flow_ops=500_000_000,
        capex=-200_000_000,
        free_cash_flow=None,
    )
    client = FakeAnthropic([_json_reply(data)])
    result = extraction.extract_filing(
        ticker="SNTS", issuer_name=None, pdf_text="text", client=client
    )
    assert result.extract.capex == 200_000_000
    # FCF derived from CFO - |capex| = 500M - 200M = 300M
    assert result.extract.free_cash_flow == 300_000_000


def test_extract_filing_derives_fcf_when_only_components_given():
    data = _happy_extract(
        cash_flow_ops=100.0,
        capex=30.0,
        free_cash_flow=None,
    )
    client = FakeAnthropic([_json_reply(data)])
    result = extraction.extract_filing(
        ticker="SNTS", issuer_name=None, pdf_text="text", client=client
    )
    assert result.extract.free_cash_flow == pytest.approx(70.0)


def test_extract_filing_prefers_reported_fcf_over_derived():
    """When the report publishes an explicit FCF line, respect it — the
    issuer may back out non-standard items we'd miss by computing."""
    data = _happy_extract(
        cash_flow_ops=100.0,
        capex=30.0,
        free_cash_flow=42.0,
    )
    client = FakeAnthropic([_json_reply(data)])
    result = extraction.extract_filing(
        ticker="SNTS", issuer_name=None, pdf_text="text", client=client
    )
    assert result.extract.free_cash_flow == pytest.approx(42.0)


def test_extract_filing_leaves_fcf_none_when_no_components():
    data = _happy_extract()  # nothing cash-flow-related
    client = FakeAnthropic([_json_reply(data)])
    result = extraction.extract_filing(
        ticker="SNTS", issuer_name=None, pdf_text="text", client=client
    )
    assert result.extract.cash_flow_ops is None
    assert result.extract.capex is None
    assert result.extract.free_cash_flow is None
