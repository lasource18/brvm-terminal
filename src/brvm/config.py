"""Runtime configuration, loaded from environment / .env via pydantic-settings."""

from __future__ import annotations

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
    db_path: str = "./data/brvm.sqlite"
    user_tz: str = "America/Montreal"

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

    http_user_agent: str = Field(default="brvm-terminal/0.1 (+contact: cmguinan@yahoo.fr)")
    http_timeout_s: float = 15.0

    @property
    def has_api_provider(self) -> bool:
        return bool(self.brvm_api_base and self.brvm_api_key)

    @property
    def has_llm(self) -> bool:
        return bool(self.anthropic_api_key)


settings = Settings()
