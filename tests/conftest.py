from typing import Any

import pytest
from pydantic import SecretStr

from core.settings import Settings

VALID: dict[str, Any] = {
    "env": "test",
    "app_name": "agentpay-guard-test",
    "app_version": "9.9.9",
    "database_url": "postgresql+psycopg://user:pw@localhost:5432/agentpay_test",
    "temporal_address": "localhost:7233",
    "otel_exporter_endpoint": "http://localhost:4318",
}


def make_settings(**overrides: Any) -> Settings:
    """Settings from explicit values only. `_env_file=None` keeps a developer's .env
    out of tests."""
    return Settings(_env_file=None, **{**VALID, **overrides})


@pytest.fixture
def settings() -> Settings:
    return make_settings(anthropic_api_key=SecretStr("sk-test-do-not-print"))
