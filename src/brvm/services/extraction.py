"""Fundamentals extractor (Phase 4b).

Turns one filing PDF into a validated `FundamentalsExtract` — the P&L
core, the business/geo segments, and the ownership register. The Haiku
call is deliberately per-filing rather than batched: an annual report is
30-50k input tokens on its own, orders of magnitude bigger than a news
batch, and mixing filings in one prompt would just risk cross-report
contamination in the model's output.

Design notes
------------
* **Pre-flight cost estimate.** We count characters in the extracted text
  and divide by 4 (Anthropic's rule-of-thumb for input tokens). If the
  estimate would push the day past `settings.llm_extract_daily_cap_cents`,
  the worker queues the filing for tomorrow. The heuristic errs high on
  purpose — better to defer a filing than to blow the cap.
* **pypdf, not OCR.** Scanned PDFs are detected by an empty text extract
  and skipped with `is_scanned=1` so a re-run doesn't keep trying. Real
  OCR is on the backlog.
* **Structured output.** Same shape as 3b: JSON schema for the wire
  format, local validation for the semantics (clamp `share_pct`, drop
  negative revenues, filter obviously bogus years).
* **Retry-on-parse-failure.** One corrective retry with the bad reply fed
  back. A `max_tokens` truncation or a refusal raises rather than
  retrying — a verbatim retry can't fix either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from brvm.config import settings
from brvm.logging import get
from brvm.services.llm import (
    LLMResponseError,
    LLMUnavailable,
    Usage,
    _usage_from_response,
    get_client,
    price_per_mtok,
)

log = get(__name__)

# Anthropic's public rule of thumb: ~1 token per 4 characters for English/
# French prose. Slightly under-counts long numbers (tokenized as digits)
# but that biases the pre-flight toward *over*-estimating cost, which is
# the safe direction for a budget gate.
_CHARS_PER_TOKEN = 4

# Trim the PDF text sent to Haiku so a huge sustainability annex doesn't
# blow one filing past the cap on its own. 120k chars ≈ 30k input tokens,
# which comfortably covers the P&L + notes + shareholder section of every
# BOA annual report in the 4a corpus.
_MAX_TEXT_CHARS = 120_000

# When the estimated call cost is above the day's remaining budget we
# refuse to send. `estimate_headroom_micros` compares against this.

_MICROS_PER_USD = 1_000_000


# --------------------------------------------------------------------------
# View / storage models
# --------------------------------------------------------------------------


class SegmentExtract(BaseModel):
    name: str
    segment_kind: str = "business"  # 'business' | 'geo'
    revenue: float | None = None
    share_pct: float | None = None


class OwnerExtract(BaseModel):
    holder: str
    share_pct: float | None = None
    shares: int | None = None


class FundamentalsExtract(BaseModel):
    """What one call returns for one filing.

    All numeric fields are optional — coverage varies wildly by issuer,
    and the UI degrades gracefully when a section is missing. `period_kind`
    defaults to 'annual' because 4b's first pass only extracts annual
    reports (interim extraction is on the backlog)."""

    period_year: int | None = None
    period_kind: str = "annual"
    currency: str = "XOF"
    revenue: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    total_assets: float | None = None
    total_equity: float | None = None
    eps: float | None = None
    dividend_per_share: float | None = None
    # Cash-flow (Phase 7). All in FULL UNITS, same currency as the P&L.
    # `free_cash_flow` is optional: derived from CFO - capex at persist
    # time when the model omits it but supplies the components.
    cash_flow_ops: float | None = None
    capex: float | None = None
    free_cash_flow: float | None = None
    segments: list[SegmentExtract] = Field(default_factory=list)
    ownership: list[OwnerExtract] = Field(default_factory=list)


@dataclass(frozen=True)
class ExtractResult:
    extract: FundamentalsExtract
    usage: Usage
    model: str
    attempts: int


@dataclass(frozen=True)
class PreflightEstimate:
    """What we expect one call to cost before we send it."""

    text_chars: int
    est_input_tokens: int
    est_output_tokens: int
    est_usd_micros: int
    truncated: bool = False


# --------------------------------------------------------------------------
# PDF -> text
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfExtract:
    text: str
    page_count: int
    is_scanned: bool
    excerpts: list[str] = field(default_factory=list)


def extract_pdf_text(path: Path, *, max_chars: int = _MAX_TEXT_CHARS) -> PdfExtract:
    """Read a PDF's text with pypdf. Empty output => scanned.

    Truncates at `max_chars` to keep one filing inside the daily budget
    even when the annual report has a 200-page RSE annex."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    page_count = len(reader.pages)
    chunks: list[str] = []
    total = 0
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception as e:  # pragma: no cover - pypdf edge cases
            log.debug("pypdf failed to extract a page from %s: %s", path.name, e)
            continue
        if not t:
            continue
        chunks.append(t)
        total += len(t)
        if total >= max_chars:
            break

    text = "\n\n".join(chunks)[:max_chars]
    return PdfExtract(
        text=text,
        page_count=page_count,
        is_scanned=(text.strip() == ""),
    )


# --------------------------------------------------------------------------
# Pre-flight cost estimate
# --------------------------------------------------------------------------


def preflight(text: str, *, model: str | None = None, output_tokens: int = 1200) -> PreflightEstimate:
    """Estimate the cost of one extraction call, in micro-dollars.

    Uses a chars/4 heuristic for input tokens. Output tokens are budgeted
    generously (~1200) so a rich segment/ownership block still fits.
    """
    mdl = model or settings.anthropic_model
    est_in = max(1, len(text) // _CHARS_PER_TOKEN + len(_SYSTEM_PROMPT) // _CHARS_PER_TOKEN)
    in_rate, out_rate = price_per_mtok(mdl)
    usd = (est_in * in_rate + output_tokens * out_rate) / 1_000_000
    return PreflightEstimate(
        text_chars=len(text),
        est_input_tokens=est_in,
        est_output_tokens=output_tokens,
        est_usd_micros=round(usd * _MICROS_PER_USD),
        truncated=(len(text) >= _MAX_TEXT_CHARS),
    )


# --------------------------------------------------------------------------
# Prompt + schema
# --------------------------------------------------------------------------


_SYSTEM_PROMPT = """\
You extract structured fundamentals from a BRVM-listed company's financial \
filing. The input may be an annual report ("Rapport annuel", "États \
financiers - Exercice YYYY"), an interim report ("Rapport d'activités - \
1er semestre YYYY" or trimestre), or an auditor's limited-review report on \
interim figures. Source material is in French. Amounts are in XOF unless \
the report clearly reports another currency (EUR / USD comparatives \
sometimes appear).

Return ONE result object for the reporting period the filing is about.

Fields:
- period_year: the calendar year the reporting period ends in (e.g. 2024 \
for "Exercice 2024" or "1er semestre 2024"). Integer.
- period_kind: 'annual' for a full-year report; 'H1' for a first-half / \
"1er semestre" / "2eme trimestre cumulé" report; 'Q1' for "1er trimestre"; \
'Q3' for "3eme trimestre" (usually 9-month cumulative); 'other' if unclear. \
Interim reports report period-to-date figures, not annualised — return \
those as-is.
- currency: 3-letter ISO code (XOF, EUR, USD). Default XOF.
- revenue: revenue for the period. Called "Chiffre d'affaires" for \
industrials/telecoms, "Produit net bancaire" (PNB) for banks, "Primes \
émises" for insurers. Take the CONSOLIDATED figure (bilan consolidé) when \
both are shown.
- operating_income: "Résultat d'exploitation" or "Résultat brut \
d'exploitation" (RBE) or "Résultat opérationnel". Not the same as EBITDA.
- net_income: "Résultat net (part du groupe)" — always the group share, \
not the total including minorities.
- total_assets: "Total du bilan" / "Total actif". Balance-sheet line.
- total_equity: "Capitaux propres (part du groupe)".
- eps: "Bénéfice par action" (BPA/BNPA) in the report's currency. For \
interim reports this is the period-to-date EPS if the report gives one; \
otherwise leave null (do NOT annualise).
- dividend_per_share: dividend proposed for this period, per share, in \
the report's currency. Interim reports usually omit this — leave null. \
Zero if a resolution explicitly says none was paid.
- cash_flow_ops: "Flux de trésorerie liés à l'activité" / \
"Flux nets de trésorerie générés par l'activité opérationnelle" / \
"Trésorerie provenant de l'exploitation" (CFO). The consolidated \
cash-flow statement usually reports this as a subtotal after working- \
capital changes; take THAT line, not gross operating cash. Sign convention: \
positive when the business generated cash.
- capex: capital expenditure for the period. In the "Flux de trésorerie \
liés aux investissements" section look for "Acquisitions d'immobilisations" \
(corporelles + incorporelles) — the outflow line. Return the amount as a \
POSITIVE number (drop the minus sign that many French reports show). If \
only a combined "investissements" line is given, use that; do NOT try to \
split acquisitions from disposals.
- free_cash_flow: leave NULL when the report doesn't publish a "Free cash \
flow" line explicitly. When it does (some issuers give a "FCF" or "Flux \
de trésorerie disponible" figure), return that value in full units. The \
persistence layer will derive `cash_flow_ops - capex` when this is null \
and both components are present, so an unlabelled cash-flow statement \
still yields a usable FCF.
- segments: business or geographic revenue breakdown. Each item: \
{name, segment_kind ('business' | 'geo'), revenue (in the same currency), \
share_pct (0-100)}. Use whichever breakdown the report actually gives; \
often both are present.
- ownership: top shareholders as of the report's balance-sheet date. Each \
item: {holder, share_pct (0-100), shares (integer, when the report gives \
absolute counts)}. Include the "Flottant" / "Public" line if present. \
Interim activity reports rarely include a full shareholder register — \
leave empty when it isn't there.

All numeric amounts in FULL UNITS (not thousands, not millions). If the \
report says "en millions de F CFA" multiply by 1_000_000 before returning.

Rules:
- Do NOT invent figures. If a line is not in the text you were given, \
return null (or omit the segment / ownership row entirely).
- Do NOT sum sub-lines to reconstruct a total that's not in the report.
- Do NOT annualise or convert interim figures into a full-year estimate.
- Prefer the CONSOLIDATED numbers over "société mère" when both exist.\
"""


_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "period_year": {"type": ["integer", "null"]},
        "period_kind": {"type": "string", "enum": ["annual", "H1", "Q1", "Q3", "other"]},
        "currency": {"type": "string"},
        "revenue": {"type": ["number", "null"]},
        "operating_income": {"type": ["number", "null"]},
        "net_income": {"type": ["number", "null"]},
        "total_assets": {"type": ["number", "null"]},
        "total_equity": {"type": ["number", "null"]},
        "eps": {"type": ["number", "null"]},
        "dividend_per_share": {"type": ["number", "null"]},
        "cash_flow_ops": {"type": ["number", "null"]},
        "capex": {"type": ["number", "null"]},
        "free_cash_flow": {"type": ["number", "null"]},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "segment_kind": {"type": "string", "enum": ["business", "geo"]},
                    "revenue": {"type": ["number", "null"]},
                    "share_pct": {"type": ["number", "null"]},
                },
                "required": ["name", "segment_kind"],
                "additionalProperties": False,
            },
        },
        "ownership": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "holder": {"type": "string"},
                    "share_pct": {"type": ["number", "null"]},
                    "shares": {"type": ["integer", "null"]},
                },
                "required": ["holder"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["period_kind", "currency", "segments", "ownership"],
    "additionalProperties": False,
}


def build_user_payload(
    *,
    ticker: str,
    issuer_name: str | None,
    period_year_hint: int | None,
    period_kind_hint: str | None,
    pdf_text: str,
) -> str:
    header = {
        "ticker": ticker,
        "issuer_name": issuer_name,
        "period_year_hint": period_year_hint,
        "period_kind_hint": period_kind_hint,
    }
    return (
        "FILING METADATA:\n"
        + json.dumps({k: v for k, v in header.items() if v is not None}, ensure_ascii=False)
        + "\n\nFILING TEXT:\n"
        + pdf_text
    )


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _clamp_pct(v: float | None) -> float | None:
    if v is None:
        return None
    if v < 0:
        return 0.0
    if v > 100:
        return 100.0
    return float(v)


def _validate(raw_text: str) -> FundamentalsExtract:
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    try:
        extract = FundamentalsExtract.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"invalid extract: {e}") from e

    # Sanity clamps — the schema guarantees shape, this guarantees meaning.
    for seg in extract.segments:
        seg.share_pct = _clamp_pct(seg.share_pct)
        if seg.revenue is not None and seg.revenue < 0:
            seg.revenue = None
        if seg.segment_kind not in ("business", "geo"):
            seg.segment_kind = "business"
    for own in extract.ownership:
        own.share_pct = _clamp_pct(own.share_pct)

    for field_name in ("revenue", "operating_income", "total_assets", "total_equity"):
        v = getattr(extract, field_name)
        if v is not None and v < 0:
            setattr(extract, field_name, None)

    # Capex is reported as an outflow in the cash-flow statement (often
    # a negative number in the source). We store it as a positive amount
    # so the FCF derivation (`CFO - capex`) has the conventional sign.
    if extract.capex is not None:
        extract.capex = abs(extract.capex)

    # Derive free cash flow when the model omits it but supplies both
    # components. Doing this here (rather than at read time) means the
    # store is always self-consistent — a later ratio query never has to
    # branch on "was FCF present or derived?".
    if (
        extract.free_cash_flow is None
        and extract.cash_flow_ops is not None
        and extract.capex is not None
    ):
        extract.free_cash_flow = extract.cash_flow_ops - extract.capex

    # A period_year of 1900 or 3000 is a hallucination; drop it.
    if extract.period_year is not None and not (1990 <= extract.period_year <= 2100):
        extract.period_year = None

    return extract


def _response_text(response: Any) -> str:
    return "".join(
        b.text for b in getattr(response, "content", []) or [] if getattr(b, "type", "") == "text"
    )


# --------------------------------------------------------------------------
# Extraction call
# --------------------------------------------------------------------------


def extract_filing(
    *,
    ticker: str,
    issuer_name: str | None,
    pdf_text: str,
    period_year_hint: int | None = None,
    period_kind_hint: str | None = None,
    client: Any | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
    max_attempts: int = 2,
) -> ExtractResult:
    """Send one filing's text to Haiku and return the parsed extract.

    Raises `LLMResponseError` (carrying the usage billed so far) when even
    a corrective retry can't produce a parseable reply, or when the reply
    was truncated (`max_tokens`) or refused."""
    if not pdf_text.strip():
        raise ValueError("empty PDF text — refuse to call the model on nothing")

    client = client or get_client()
    model = model or settings.anthropic_model
    max_output_tokens = max_output_tokens or 2048

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": build_user_payload(
                ticker=ticker,
                issuer_name=issuer_name,
                period_year_hint=period_year_hint,
                period_kind_hint=period_kind_hint,
                pdf_text=pdf_text,
            ),
        }
    ]
    total = Usage()
    last_error = "unknown error"

    for attempt in range(1, max_attempts + 1):
        response = client.messages.create(
            model=model,
            max_tokens=max_output_tokens,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
            output_config={"format": {"type": "json_schema", "schema": _RESULT_SCHEMA}},
        )
        total = total + _usage_from_response(response, model)

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "max_tokens":
            raise LLMResponseError(
                f"extraction reply truncated at max_tokens={max_output_tokens}",
                total,
            )
        if stop_reason == "refusal":
            raise LLMResponseError("model refused the extraction request", total)

        text = _response_text(response)
        try:
            extract = _validate(text)
        except (json.JSONDecodeError, ValueError) as e:
            last_error = str(e)
            log.warning("extraction attempt %d/%d unusable: %s", attempt, max_attempts, e)
            if attempt >= max_attempts:
                break
            messages = [
                messages[0],
                {"role": "assistant", "content": text or "(empty)"},
                {
                    "role": "user",
                    "content": (
                        f"That reply could not be used: {e}. Answer again with the "
                        "same JSON object, one object matching the schema, nothing else."
                    ),
                },
            ]
            continue

        return ExtractResult(extract=extract, usage=total, model=model, attempts=attempt)

    raise LLMResponseError(
        f"no usable extraction after {max_attempts} attempts: {last_error}", total
    )


# Re-exports so callers only need `services.extraction` in their imports.
__all__ = [
    "ExtractResult",
    "FundamentalsExtract",
    "LLMResponseError",
    "LLMUnavailable",
    "OwnerExtract",
    "PdfExtract",
    "PreflightEstimate",
    "SegmentExtract",
    "Usage",
    "build_user_payload",
    "extract_filing",
    "extract_pdf_text",
    "preflight",
]
