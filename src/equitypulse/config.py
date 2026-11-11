"""Application settings, read from environment variables and `.env`.

Every key, token and password is a `SecretStr` so that printing, logging or dumping the
settings shows `**********` instead of the value. Read a secret only at the point of use,
with `.get_secret_value()`, and never pass the raw value to a logger.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ENV_FILE = Path(".env")


class ConfigError(RuntimeError):
    """Settings are missing or invalid. The message names the variable, never its value."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        # `KEY=` in .env means "not set", so defaults apply and required keys stay missing.
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    # Database (SQLite now; PostgreSQL later). Secret because a Postgres URL carries a password.
    database_url: SecretStr = SecretStr("sqlite:///data/private/equitypulse.db")

    # Tapetide MCP: required, because Phase 0 ingestion depends on it.
    tapetide_mcp_url: str = "https://mcp.tapetide.com/mcp"
    tapetide_api_token: SecretStr
    tapetide_daily_budget: int = Field(default=45, ge=0)

    # LLM (later phases): optional until those phases need them.
    openrouter_api_key: SecretStr | None = None
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "equitypulse"

    # Email alerts (later phases).
    gmail_user: str | None = None
    gmail_app_password: SecretStr | None = None
    alert_to: str | None = None

    # NSE downloads: polite delay between requests.
    nse_request_delay_seconds: float = Field(default=1.5, ge=0)

    @field_validator("tapetide_api_token", mode="before")
    @classmethod
    def _required_secret_not_blank(cls, value: Any) -> Any:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw, str) and not raw.strip():
            raise ValueError("is blank")
        return value

    @field_validator(
        "openrouter_api_key",
        "langsmith_api_key",
        "gmail_app_password",
        "gmail_user",
        "alert_to",
        mode="before",
    )
    @classmethod
    def _blank_optional_is_none(cls, value: Any) -> Any:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw, str) and not raw.strip():
            return None
        return value


def _describe(error: dict[str, Any]) -> str:
    """Name the variable and the problem; never include the input value (it may be a secret)."""
    name = str(error["loc"][0]).upper() if error.get("loc") else "<settings>"
    if error.get("type") == "missing":
        return f"{name} is missing"
    return f"{name}: {error.get('msg', 'invalid value')}"


def load_settings(env_file: Path | str | None = DEFAULT_ENV_FILE) -> Settings:
    """Build settings, turning pydantic's error (which echoes inputs) into a safe, clear one."""
    try:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as exc:
        problems = "; ".join(_describe(e) for e in exc.errors())
        raise ConfigError(
            f"Invalid configuration: {problems}. "
            "Set it in .env (see .env.example) or as an environment variable."
        ) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, loaded once from the default `.env`."""
    return load_settings()
