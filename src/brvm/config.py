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

    anthropic_api_key: str = ""

    http_user_agent: str = Field(default="brvm-terminal/0.1 (+contact: cmguinan@yahoo.fr)")
    http_timeout_s: float = 15.0

    @property
    def has_api_provider(self) -> bool:
        return bool(self.brvm_api_base and self.brvm_api_key)


settings = Settings()
