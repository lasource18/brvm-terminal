"""Thin Anthropic client for the news-tagging pipeline (Phase 3b).

Scope is deliberately narrow: one call shape (tag a batch of news items),
strict JSON out, and exact token/cost accounting so `services/tagging.py`
can enforce the charter's $1/day hard cap.

Design notes
------------
* **Batching.** One request carries up to `settings.llm_batch_size` items
  and returns one result per item, keyed by the item's DB id. The system
  prompt (instructions + the whole BRVM ticker universe) is by far the
  biggest part of the payload, so amortizing it across a batch is where
  the cost saving comes from.
* **Structured output.** `output_config.format` with a JSON schema means
  the model can't hand back prose or a fenced code block, so there is no
  regex-extraction step. We still validate (ids, tickers, ranges) because
  a schema guarantees shape, not semantics.
* **Prompt caching.** The system prompt is stable across a whole tagging
  pass (it only changes when the securities table does), so it carries a
  `cache_control` breakpoint. Below the ~1024-token minimum the API
  simply doesn't cache and nothing extra is charged.
* **The SDK import is lazy** — `anthropic` pulls in a fair amount, and
  the web app imports the scheduler (and therefore this module's callers)
  on every boot. We only pay for it when a tagging pass actually runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from brvm.config import settings
from brvm.logging import get

log = get(__name__)

CATEGORIES: tuple[str, ...] = (
    "earnings",
    "dividend",
    "governance",
    "macro",
    "capital_action",
    "other",
)

# USD per 1M tokens, (input, output). Keyed by model-id prefix so a dated
# snapshot ("claude-haiku-4-5-20251001") resolves to its family's price.
# Cache reads bill at 0.1x input, cache writes at 1.25x input.
_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}
_FALLBACK_PRICE = (1.00, 5.00)
_CACHE_READ_MULTIPLIER = 0.10
_CACHE_WRITE_MULTIPLIER = 1.25

_MICROS_PER_USD = 1_000_000


class LLMUnavailable(RuntimeError):
    """No ANTHROPIC_API_KEY configured (or the SDK isn't installed)."""


class LLMResponseError(RuntimeError):
    """The model answered but the answer was unusable.

    Carries the `Usage` for the attempts that were made — those tokens
    were billed whether or not we could use the reply, so the caller must
    still record them against the daily budget.
    """

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage or Usage()


@dataclass(frozen=True)
class TagItem:
    """One news row on its way to the model."""

    id: int
    title: str
    kind: str = "news"
    source: str = ""
    chapeau: str | None = None
    issuer_name: str | None = None
    published_at: str | None = None
    ticker_hint: str | None = None


class NewsTag(BaseModel):
    """Validated per-item tag coming back from the model."""

    id: int
    tickers: list[str] = Field(default_factory=list)
    relevance: int = 0
    category: str = "other"
    summary_fr: str = ""
    summary_en: str = ""


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd_micros: int = 0
    calls: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            usd_micros=self.usd_micros + other.usd_micros,
            calls=self.calls + other.calls,
        )


@dataclass(frozen=True)
class TagBatchResult:
    tags: list[NewsTag]
    usage: Usage
    model: str
    attempts: int


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


def price_per_mtok(model: str) -> tuple[float, float]:
    """(input, output) USD per 1M tokens for `model`, longest prefix wins."""
    best: tuple[str, tuple[float, float]] | None = None
    for prefix, price in _PRICES_PER_MTOK.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, price)
    if best is None:
        log.warning("no price entry for model %r; billing at Haiku rates", model)
        return _FALLBACK_PRICE
    return best[1]


def usd_micros_for(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> int:
    """Cost of one call in micro-dollars (1 USD = 1_000_000 micros).

    Sub-cent precision matters: a single tagging batch runs well under a
    tenth of a cent, and integer cents would round every call to zero.
    """
    in_rate, out_rate = price_per_mtok(model)
    usd = (
        input_tokens * in_rate
        + cache_read_tokens * in_rate * _CACHE_READ_MULTIPLIER
        + cache_write_tokens * in_rate * _CACHE_WRITE_MULTIPLIER
        + output_tokens * out_rate
    ) / 1_000_000
    return round(usd * _MICROS_PER_USD)


def usage_from_response(response: Any, model: str) -> Usage:
    """Extract token counts + priced cost from an Anthropic response.
    Public since Phase 6b — the brief writer reuses it verbatim."""
    u = getattr(response, "usage", None)
    input_tokens = int(getattr(u, "input_tokens", 0) or 0)
    output_tokens = int(getattr(u, "output_tokens", 0) or 0)
    cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        usd_micros=usd_micros_for(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        ),
        calls=1,
    )


_usage_from_response = usage_from_response


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

_INSTRUCTIONS = """\
You tag news about the BRVM (Bourse Régionale des Valeurs Mobilières), the \
regional stock exchange of the 8 WAEMU countries, based in Abidjan. Source \
material is in French. Prices are in XOF (CFA franc).

For every item in the INPUT array, produce exactly one result object.

Fields:
- id: echo the item's id unchanged. Never invent ids, never merge items.
- tickers: the BRVM tickers the item is materially ABOUT, taken only from \
the TICKER UNIVERSE below. Use the ticker code, never the company name. \
Empty list if the item is macro, sector-wide, or about an unlisted company. \
Do not add a ticker just because the company is mentioned in passing.
- relevance: 0-10 for an investor holding BRVM securities.
  0-2 trivial or purely promotional; 3-5 background/macro colour;
  6-8 materially useful (results, dividends, board changes, guidance);
  9-10 market-moving (large earnings surprise, capital raise, suspension,
  takeover, default).
- category: exactly one of earnings, dividend, governance, macro, \
capital_action, other.
  earnings: results, revenue, profit, activity indicators, guidance.
  dividend: dividend declarations, ex-dates, payment dates, coupons.
  governance: board/CEO changes, AGM/EGM convocations, regulatory sanctions,
  auditor matters.
  macro: WAEMU/BCEAO/sovereign, rates, economy, exchange-wide statistics,
  index moves.
  capital_action: capital increase, bond or share issue, IPO, buyback, split,
  merger, listing or delisting.
  other: anything that fits none of the above.
- summary_fr: 1-2 sentences, French, factual. Keep company names, figures and
  dates as written in the source. No preamble, no "cet article".
- summary_en: the same content in English, 1-2 sentences.

Rules:
- Base the tags only on the title, chapeau and issuer name supplied. Do not
  speculate about content you were not given, and never invent figures.
- A "communiqué" item is a company filing (PDF); its issuer_name is reliable.
- If ticker_hint is present it was resolved from an exact company-name match
  and is almost always right — keep it unless the text clearly contradicts it.
- Return results for every input id, in the same order.\
"""


def build_system_prompt(universe: list[tuple[str, str, str | None]]) -> str:
    """Instructions + the live ticker table.

    The universe is read from `securities` (never hardcoded, per the
    charter) so newly-listed companies become taggable as soon as the
    reference-data job picks them up.
    """
    lines = [f"{t}\t{name}" + (f"\t{sector}" if sector else "") for t, name, sector in universe]
    return f"{_INSTRUCTIONS}\n\nTICKER UNIVERSE (ticker, name, sector):\n" + "\n".join(lines)


def build_user_payload(items: list[TagItem]) -> str:
    payload = [
        {
            k: v
            for k, v in {
                "id": it.id,
                "kind": it.kind,
                "source": it.source,
                "published_at": it.published_at,
                "issuer_name": it.issuer_name,
                "ticker_hint": it.ticker_hint,
                "title": it.title,
                "chapeau": it.chapeau,
            }.items()
            if v not in (None, "")
        }
        for it in items
    ]
    return "INPUT:\n" + json.dumps(payload, ensure_ascii=False, indent=None)


_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "tickers": {"type": "array", "items": {"type": "string"}},
                    # Anthropic's output_config schema validator rejects
                    # `minimum` / `maximum` on integer types. The [0, 10]
                    # invariant is still enforced locally by `_validate`.
                    "relevance": {"type": "integer"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "summary_fr": {"type": "string"},
                    "summary_en": {"type": "string"},
                },
                "required": [
                    "id",
                    "tickers",
                    "relevance",
                    "category",
                    "summary_fr",
                    "summary_en",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

_client: Any = None


def get_client() -> Any:
    """Lazily build (and memoize) the Anthropic client."""
    global _client
    if _client is not None:
        return _client
    if not settings.anthropic_api_key:
        raise LLMUnavailable(
            "ANTHROPIC_API_KEY is not set — news tagging is disabled. "
            "Add it to .env to enable the Phase 3b tagger."
        )
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - dependency is pinned
        raise LLMUnavailable(f"anthropic SDK not installed: {e}") from e
    _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def reset_client() -> None:
    """Drop the memoized client (used by tests and after a config reload)."""
    global _client
    _client = None


# --------------------------------------------------------------------------
# Tagging call
# --------------------------------------------------------------------------


def response_text(response: Any) -> str:
    """Concatenate the `text` content blocks of an Anthropic response.
    Public since Phase 6b — the brief writer reuses it verbatim."""
    return "".join(
        b.text for b in getattr(response, "content", []) or [] if getattr(b, "type", "") == "text"
    )


# Back-compat alias for pre-6b callers inside this module.
_response_text = response_text


def _validate(raw_text: str, wanted_ids: set[int], allowed_tickers: set[str]) -> list[NewsTag]:
    """Parse + sanity-check the model's JSON.

    The schema guarantees shape; this guarantees meaning — ids we asked
    for, tickers that actually exist, relevance in range.
    """
    data = json.loads(raw_text)  # JSONDecodeError bubbles to the retry loop
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("expected an object with a 'results' array")

    tags: list[NewsTag] = []
    seen: set[int] = set()
    for entry in data["results"]:
        try:
            tag = NewsTag.model_validate(entry)
        except ValidationError as e:
            raise ValueError(f"invalid result entry: {e}") from e
        if tag.id not in wanted_ids:
            log.warning("dropping tag for unknown item id %s", tag.id)
            continue
        if tag.id in seen:
            log.warning("dropping duplicate tag for item id %s", tag.id)
            continue
        seen.add(tag.id)

        kept = [t for t in (x.strip().upper() for x in tag.tickers) if t in allowed_tickers]
        dropped = len(tag.tickers) - len(kept)
        if dropped:
            log.debug("item %s: dropped %d ticker(s) outside the universe", tag.id, dropped)
        tag.tickers = list(dict.fromkeys(kept))
        tag.relevance = max(0, min(10, tag.relevance))
        if tag.category not in CATEGORIES:
            tag.category = "other"
        tags.append(tag)

    if not tags:
        raise ValueError("no usable results in reply")
    return tags


def tag_batch(
    items: list[TagItem],
    universe: list[tuple[str, str, str | None]],
    *,
    client: Any | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
    max_attempts: int = 2,
) -> TagBatchResult:
    """Tag one batch of news items in a single API call.

    Retries once on an unusable reply (the charter's
    "retry-on-parse-failure"), feeding the parse error back so the model
    can correct itself. Raises `LLMResponseError` — carrying the usage
    billed so far — when even the retry fails, and on a truncated
    (`max_tokens`) or refused reply, neither of which a verbatim retry
    would fix.
    """
    if not items:
        return TagBatchResult(
            tags=[], usage=Usage(), model=model or settings.anthropic_model, attempts=0
        )

    client = client or get_client()
    model = model or settings.anthropic_model
    max_output_tokens = max_output_tokens or settings.llm_max_output_tokens

    wanted_ids = {it.id for it in items}
    allowed_tickers = {t.upper() for t, _name, _sector in universe}

    messages: list[dict[str, Any]] = [{"role": "user", "content": build_user_payload(items)}]
    total = Usage()
    last_error = "unknown error"

    for attempt in range(1, max_attempts + 1):
        response = client.messages.create(
            model=model,
            max_tokens=max_output_tokens,
            system=[
                {
                    "type": "text",
                    "text": build_system_prompt(universe),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
            output_config={"format": {"type": "json_schema", "schema": _RESULT_SCHEMA}},
        )
        total = total + _usage_from_response(response, model)

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "max_tokens":
            # A verbatim retry truncates the same way; the caller should
            # split the batch instead.
            raise LLMResponseError(
                f"reply truncated at max_tokens={max_output_tokens} for {len(items)} items",
                total,
            )
        if stop_reason == "refusal":
            raise LLMResponseError("model refused to answer the tagging request", total)

        text = _response_text(response)
        try:
            tags = _validate(text, wanted_ids, allowed_tickers)
        except (json.JSONDecodeError, ValueError) as e:
            last_error = str(e)
            log.warning("tagging attempt %d/%d unusable: %s", attempt, max_attempts, e)
            if attempt >= max_attempts:
                break
            messages = [
                messages[0],
                {"role": "assistant", "content": text or "(empty)"},
                {
                    "role": "user",
                    "content": (
                        f"That reply could not be used: {e}. Answer again with the "
                        "same JSON object, one result per input id, nothing else."
                    ),
                },
            ]
            continue

        missing = wanted_ids - {t.id for t in tags}
        if missing:
            log.warning("model returned no tag for item ids %s", sorted(missing))
        return TagBatchResult(tags=tags, usage=total, model=model, attempts=attempt)

    raise LLMResponseError(f"no usable reply after {max_attempts} attempts: {last_error}", total)
