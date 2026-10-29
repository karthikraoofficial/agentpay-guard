"""The single place configuration is read (CLAUDE.md non-negotiable 9).

Values come from the process environment or a local `.env` file, prefixed
`AGENTPAY_`. Every other module receives a `Settings` object by injection
(see `api.main.create_app`) or through `get_settings()`, and never reads the
environment itself. Required values have no default, so a missing or
malformed one fails at startup rather than at the first request.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTPAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    app_name: str = "agentpay-guard"
    app_version: str = "0.0.1"
    env: Literal["local", "test", "staging", "production"]

    database_url: AnyUrl
    temporal_address: str = Field(pattern=r"^[\w.\-]+:\d+$")
    otel_exporter_endpoint: AnyUrl

    # Released Policy packs and their Manifest. The active `default` pack is loaded and
    # hash-checked at startup; a mismatch stops the app (ADR-0002).
    policy_packs_dir: Path = Path("policy_packs")

    # Optional until Phase 2 (intent check). SecretStr keeps the value out of repr and logs.
    anthropic_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    """Load settings from the environment once. Raises ValidationError if invalid."""
    return Settings()  # required fields come from the environment
