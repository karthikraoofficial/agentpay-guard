from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, make_url, text

from core.settings import Settings, get_settings

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


@pytest.fixture(scope="session")
def migrated_database_url() -> Iterator[str]:
    """A freshly created `<database>_test` database on the configured Postgres server,
    migrated to head. Recreated once per test session; needs local infra (or CI's
    Postgres service) running."""
    configured = make_url(str(get_settings().database_url))
    test_url = configured.set(database=f"{configured.database}_test")
    admin = create_engine(configured.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{test_url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{test_url.database}"'))
    admin.dispose()

    url = test_url.render_as_string(hide_password=False)
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    yield url
