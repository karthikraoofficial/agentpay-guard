"""The single place configuration is read (CLAUDE.md non-negotiable 9).

Every other module receives settings through `get_settings()`; nothing else
reads environment variables. Values come from the process environment or a
local `.env` file, prefixed `AGENTPAY_`.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTPAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["local", "test", "staging", "production"] = "local"
    service_name: str = "agentpay-guard"

    database_url: str = "postgresql+psycopg://agentpay:agentpay@localhost:5432/agentpay"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    otel_exporter_endpoint: str = "http://localhost:4318"

    anthropic_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
