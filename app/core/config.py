"""Application configuration.

Secrets are only ever read from the environment (``VIBE_API_TOKEN``,
``VIBE_WEBHOOK_SECRET``) and are held as :class:`~pydantic.SecretStr` so that an
accidental ``repr``/log of the settings object cannot leak them.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppMode(StrEnum):
    """How much of the real, billable API the process is allowed to touch."""

    MOCK = "mock"
    ESTIMATE = "estimate"
    LIVE = "live"

    @property
    def uses_network(self) -> bool:
        return self is not AppMode.MOCK

    @property
    def allows_spending(self) -> bool:
        return self is AppMode.LIVE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- mode / observability ---------------------------------------------
    app_mode: AppMode = AppMode.MOCK
    app_env: str = "local"
    log_level: str = "INFO"
    log_format: str = "json"

    # --- upstream API ------------------------------------------------------
    vibe_base_url: str = "https://lk.vibemarketolog.ru/api/agent"
    vibe_api_token: SecretStr | None = None
    vibe_webhook_secret: SecretStr | None = None
    vibe_webhook_legacy_fallback: bool = False
    callback_base_url: str | None = None

    # --- http / retry / polling -------------------------------------------
    http_connect_timeout: float = 5.0
    http_read_timeout: float = 30.0
    http_total_timeout: float = 60.0
    retry_max_attempts: int = Field(default=4, ge=1, le=10)
    retry_base_delay: float = Field(default=0.5, gt=0)
    retry_max_delay: float = Field(default=8.0, gt=0)
    retry_jitter: float = Field(default=0.3, ge=0, le=1)
    poll_interval_seconds: float = Field(default=10.0, gt=0)
    poll_timeout_seconds: float = Field(default=900.0, gt=0)

    # --- background execution ----------------------------------------------
    #: How many confirmed plans may execute at once. Each job is claimed in the
    #: database first, so raising this cannot cause a plan to be executed twice.
    executor_concurrency: int = Field(default=2, ge=1, le=16)

    # --- budget guardrails -------------------------------------------------
    max_budget_rub: float = Field(default=10_000.0, gt=0)
    budget_safety_margin: float = Field(default=0.05, ge=0, lt=0.5)

    # --- storage -----------------------------------------------------------
    db_path: Path = Path("./data/vibe_agent.db")

    # --- mock account simulation ------------------------------------------
    mock_balance_rub: float = 5_000.0
    mock_daily_limit_rub: float = 5_000.0
    mock_daily_spent_rub: float = 0.0

    # --- model policy ------------------------------------------------------
    policy_file: Path | None = None

    @field_validator(
        "vibe_api_token",
        "vibe_webhook_secret",
        "callback_base_url",
        "policy_file",
        mode="before",
    )
    @classmethod
    def _blank_means_unset(cls, value: Any) -> Any:
        """An empty variable in ``.env`` means "not configured", not an empty value.

        ``.env.example`` ships these keys empty (``VIBE_API_TOKEN=``), so without this
        an empty ``POLICY_FILE=`` would become ``Path(".")`` and an empty token would
        look like a configured secret.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_mode_requirements(self) -> Settings:
        if self.app_mode.uses_network and not self.vibe_api_token:
            raise ValueError(
                f"APP_MODE={self.app_mode.value} requires VIBE_API_TOKEN to be set. "
                "Use APP_MODE=mock for a keyless local demo."
            )
        if self.callback_base_url:
            self.callback_base_url = self.callback_base_url.rstrip("/")
        return self

    @property
    def token_value(self) -> str | None:
        return self.vibe_api_token.get_secret_value() if self.vibe_api_token else None

    @property
    def webhook_secret_value(self) -> str | None:
        return (
            self.vibe_webhook_secret.get_secret_value() if self.vibe_webhook_secret else None
        )

    @property
    def callback_url(self) -> str | None:
        """Public URL the platform should POST generation callbacks to."""
        if not self.callback_base_url:
            return None
        return f"{self.callback_base_url}/api/v1/webhooks/vibe"

    def secret_values(self) -> list[str]:
        """Every secret string the log redactor must scrub."""
        return [v for v in (self.token_value, self.webhook_secret_value) if v]


@lru_cache
def get_settings() -> Settings:
    return Settings()
