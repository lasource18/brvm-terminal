"""Machine translation of LLM-generated markdown (PR-I).

Purpose: the daily brief and weekly analyst notes are synthesised in
English (the two prompts pin the output language) but the UI supports
FR/EN. Rather than translate on every render (blocks the page, pays
per-view), we translate once at write time and cache the result on the
row (`markdown_fr`).

Design notes
------------
* **Haiku by default.** Translation is a much simpler task than the
  original synthesis; Haiku is fast, cheap (~$1/M tokens), and preserves
  markdown structure reliably. Sonnet-authored notes still translate on
  Haiku — the note's *reasoning* was Sonnet-grade, the translation of
  the finished prose is not.
* **Cache breakpoint on the system prompt.** The instructions block is
  stable across every translation; putting it in the cached section
  saves the input-token bill on runs that batch multiple translations
  in the same day (brief + N analyst notes).
* **Preserve headings, tickers, numbers.** The prompt is explicit about
  keeping `# ...` headings, ticker codes (uppercase alnum), and numeric
  formatting untouched. The UI's markdown renderer relies on the
  headings shape (`# Session recap`, etc.) to style sections.
* **Failure is soft.** Translation errors don't propagate — the caller
  writes the source markdown regardless and the read path falls back
  with a "translation pending" badge. A later brief/note re-run gets
  another shot at translation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kodji.config import settings
from kodji.logging import get
from kodji.services import llm as llm_svc

log = get(__name__)


# The translation model. Haiku is the right default; keeping it as a
# module constant (not a `Settings` field) because there's no operational
# reason for it to differ across brief/note callers — if that changes,
# lift it into config.
_TRANSLATION_MODEL_DEFAULT = "claude-haiku-4-5-20251001"
_TRANSLATION_MAX_TOKENS_DEFAULT = 4096

# System prompt is stable across runs — carries `cache_control` so the
# 90% cache-read discount kicks in as soon as multiple translations run
# in the same batch (brief + N notes on a nightly pass).
_SYSTEM_PROMPT_FR = """\
You translate financial-market markdown from English to French for a \
BRVM (Bourse Régionale des Valeurs Mobilières) reader. Output ONLY the \
translated markdown — no preamble, no sign-off, no code fences around \
the result.

Rules:
- Preserve the markdown structure exactly: keep every `#`/`##` heading, \
list bullet (`-` / `*`), bold/italic marker, and link intact. The reader's \
UI relies on the heading shape.
- Do NOT translate ticker codes (SNTS, ORAC, BOAD.O11, CRRH.O7, ...), \
ISO country codes (CI, SN, ...), currency codes (XOF, EUR, USD), or \
proper nouns of companies and indices.
- Keep numbers, percentages, and dates in their original form. A French \
reader accepts `12.5%` and `2026-08-29` as-is; changing formatting risks \
introducing errors.
- Use standard French financial vocabulary: \
"chiffre d'affaires" for revenue, "résultat net" for net income, \
"BPA" for EPS, "rendement" for yield, "capitalisation boursière" for \
market cap, "flux de trésorerie disponible" for free cash flow, \
"marge nette" / "marge opérationnelle" for margins.
- Translate section headings as follows: "Session recap" → \
"Résumé de séance", "Movers" → "Principaux mouvements", \
"News that matters" → "Actualités marquantes", \
"Watch tomorrow" → "À surveiller demain". Other headings: use natural \
French financial phrasing.
- Keep the prose tight — same length as the source, same tone \
(neutral, factual).\
"""


@dataclass(frozen=True)
class TranslationResult:
    """Outcome of one translation call."""

    text: str
    usage: llm_svc.Usage
    model: str


def _build_client_payload(source_text: str) -> str:
    return "SOURCE MARKDOWN (English):\n\n" + source_text


def translate_markdown_to_fr(
    source_text: str,
    *,
    client: Any | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
) -> TranslationResult:
    """Translate `source_text` (English markdown) into French markdown.

    Returns a `TranslationResult` on success. Raises:
    - `llm_svc.LLMUnavailable` if no API key is configured (caller should
      catch and skip translation without failing the primary write)
    - `llm_svc.LLMResponseError` if the model responds with empty text
      (the caller must still bill the usage that was spent)

    Transport errors bubble up as-is so the caller decides whether to
    retry or absorb them.
    """
    if not source_text.strip():
        # Nothing to translate. Return the input as-is with zero-billing
        # usage so callers can persist it without a network round-trip.
        return TranslationResult(
            text=source_text,
            usage=llm_svc.Usage(),
            model=model or _TRANSLATION_MODEL_DEFAULT,
        )

    client = client or llm_svc.get_client()
    model_id = model or _TRANSLATION_MODEL_DEFAULT
    max_tokens = max_output_tokens or _TRANSLATION_MAX_TOKENS_DEFAULT

    response = client.messages.create(
        model=model_id,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT_FR,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {"role": "user", "content": _build_client_payload(source_text)},
        ],
    )
    usage = llm_svc.usage_from_response(response, model_id)
    text = llm_svc.response_text(response).strip()

    if not text:
        raise llm_svc.LLMResponseError(
            "empty reply from translation model", usage=usage
        )
    return TranslationResult(text=text, usage=usage, model=model_id)


def has_llm() -> bool:
    """Whether we can attempt a translation call. Callers use this to
    skip translation cleanly when no key is configured — the primary
    write still lands, the FR column stays NULL, and the UI badge
    surfaces the pending state."""
    return settings.has_llm


__all__ = [
    "TranslationResult",
    "has_llm",
    "translate_markdown_to_fr",
]
