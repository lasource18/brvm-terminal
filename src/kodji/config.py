"""Runtime configuration, loaded from environment / .env via pydantic-settings.

`settings` is a lazy proxy: on the first attribute access it builds a
`Settings()` instance and caches it. Tests (and the CLI) can flip envs
and then call `reset_settings_cache()` to force a fresh read on the next
attribute access — no `importlib.reload` sweep needed.

The proxy is intentionally minimal (attribute forwarding + `__contains__`
for `hasattr`-style checks); nothing else in the codebase introspects the
Settings object.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    log_level: str = "INFO"
    db_path: str = "./data/kodji.sqlite"

    brvm_api_base: str = ""
    brvm_api_key: str = ""

    # --- LLM (news tagging, Phase 3b) ---
    anthropic_api_key: str = ""
    # Charter pins Haiku for high-volume tagging. Dated snapshot so a silent
    # alias re-point can't change tagging behaviour (or cost) under us.
    anthropic_model: str = "claude-haiku-4-5-20251001"
    # Hard daily ceiling, USD cents. The tagging worker no-ops once the day's
    # accumulated spend crosses this (see store/spend.py).
    llm_daily_cap_cents: int = 100
    # News items per Haiku request. Bigger batches amortize the system prompt;
    # too big and one bad item costs the whole batch on a retry.
    llm_batch_size: int = 8
    llm_max_output_tokens: int = 4096
    # Give up on a tagging pass after this many consecutive failed batches.
    llm_max_consecutive_failures: int = 3

    # --- Filings extractor (Phase 4b) — settings land now so 4a can round-trip
    # the corpus + spend counter without a config bump later. ---
    # Separate daily cap from `llm_daily_cap_cents` because an annual report
    # is ~30-50k input tokens per call, i.e. orders of magnitude bigger than
    # a news-tagging batch. Charter's news budget stays untouched.
    llm_extract_daily_cap_cents: int = 200
    # Refuse to download PDFs bigger than this — protects the VPS disk and
    # cuts off obvious junk (scanned corpuses, marketing decks).
    extract_max_pdf_mb: int = 25
    # Where downloaded filings land. Relative paths resolve from the project
    # root at write time; absolute paths are honoured as-is.
    filings_root: str = "./data/filings"

    # --- OCR (Phase 4c) — scanned-PDF rescue via ocrmypdf. Free (CPU-only),
    # so no daily cap, but do bound per-file time so a pathological PDF can't
    # eat the whole night. Requires the `ocrmypdf` binary and tesseract with
    # the French language pack (see README).
    ocr_binary: str = "ocrmypdf"
    ocr_languages: str = "fra+eng"
    ocr_timeout_s: int = 600           # per-file wall-clock cap
    ocr_max_pages: int = 400           # skip filings larger than this (heavy)
    ocr_max_files_per_run: int = 20    # limit one pass to ~20 files (~1-2h)

    # --- Alerts (Phase 6a) ---
    # Optional Discord webhook. Empty → alerts still fire and land in
    # alert_events, but nothing is pushed out (delivery worker no-ops with
    # a warning). Any legitimate webhook URL is fine — we don't parse it,
    # just POST JSON.
    discord_webhook_url: str = ""
    # A rule that keeps re-matching (e.g. SNTS holds a +6% day for hours)
    # only fires once per this window. Same (rule_id, dedupe_key) inside the
    # window is dropped at the store layer.
    alerts_dedupe_window_hours: int = 24
    # Cap events queued for one delivery pass so a webhook outage doesn't
    # produce a 50-message avalanche when it recovers.
    alerts_delivery_batch: int = 10

    # --- Daily brief (Phase 6b) ---
    # Starts on Haiku (matches the 3b tagger — same $/tok, dated snapshot);
    # can be flipped to Sonnet later if the write-up quality warrants it.
    brief_model: str = "claude-haiku-4-5-20251001"
    # $0.50/day cap. One brief per weekday, ~2k output tokens on a normal
    # day; Haiku prices this at fractions of a cent, so the cap is mostly
    # a safety net for a run-away prompt.
    brief_daily_cap_cents: int = 50
    # News-item floor for what the brief writer sees. Below this we drop
    # the row entirely from the model's context so the prompt stays lean.
    brief_min_relevance: int = 6
    # Hard cap on news items handed to the model. A very newsy day (e.g.
    # results season) can produce dozens of high-relevance items; more
    # than this and we truncate to the most-relevant first.
    brief_max_news_items: int = 30
    # Output tokens ceiling. Two thousand is comfortable for a ~500-word
    # markdown brief.
    brief_max_output_tokens: int = 2048

    # --- Analyst notes (Phase 6c) ---
    # Weekly per-ticker synthesis. Sonnet by user choice — an analyst
    # note is a much richer write than a daily-brief summary and
    # benefits from the deeper reasoning. The full weekly pass at
    # ~$0.04/ticker x 47 equities ≈ $1.90; the daily cap gives one
    # full retry of headroom.
    notes_model: str = "claude-sonnet-4-6"
    notes_daily_cap_cents: int = 300
    # News lookback per ticker for the prompt context. 30 days catches
    # a full month of communiqués + a typical earnings cycle.
    notes_lookback_days: int = 30
    # Ceiling on news items fed to the model per ticker. Bigger prompts
    # cost linearly; 25 is enough to cover a busy earnings week.
    notes_max_news_items: int = 25
    # Output tokens ceiling — Sonnet writes longer than Haiku by
    # default; 3k is comfortable for a 1000-1200-word note with several
    # sections.
    notes_max_output_tokens: int = 3072
    # Polite pause between per-ticker calls so a full 47-ticker weekly
    # pass doesn't hammer the API in a burst.
    notes_delay_between_s: float = 0.5

    http_user_agent: str = Field(default="kodji-terminal/0.1 (+contact: cmguinan@yahoo.fr)")
    http_timeout_s: float = 15.0

    @property
    def has_api_provider(self) -> bool:
        return bool(self.brvm_api_base and self.brvm_api_key)

    @property
    def has_llm(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_discord(self) -> bool:
        return bool(self.discord_webhook_url)

    @property
    def brief_daily_cap_micros(self) -> int:
        return self.brief_daily_cap_cents * 10_000

    @property
    def notes_daily_cap_micros(self) -> int:
        return self.notes_daily_cap_cents * 10_000


_cached: Settings | None = None


def _load() -> Settings:
    global _cached
    if _cached is None:
        _cached = Settings()
    return _cached


def reset_settings_cache() -> None:
    """Drop the cached Settings so the next attribute access rebuilds it
    from the current environment. Tests call this after monkeypatching
    envs; production code should never need it."""
    global _cached
    _cached = None


class _SettingsProxy:
    """Attribute-forwarding proxy for the cached Settings instance.

    Kept intentionally minimal: nothing in the codebase does
    `isinstance(settings, Settings)` or introspects `.model_*`, so
    attribute access is all we need to preserve.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(_load(), name)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SettingsProxy {_load()!r}>"


settings = _SettingsProxy()
