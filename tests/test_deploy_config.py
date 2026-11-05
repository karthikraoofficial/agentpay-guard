"""The hosted demo's settings (#139, ADR-0021): `.env.production.example` lists real
settings only, and they load."""

from pathlib import Path

from core.settings import Settings

EXAMPLE = Path(".env.production.example")
# Read by docker compose, never by the app.
COMPOSE_ONLY = {
    "AGENTPAY_API_HOST",
    "AGENTPAY_POSTGRES_PASSWORD",
    "AGENTPAY_LANGFUSE_PUBLIC_KEY",
    "AGENTPAY_LANGFUSE_SECRET_KEY",
    "AGENTPAY_LANGFUSE_HOST",
}
# Set by infra/docker-compose.prod.yml itself.
FROM_COMPOSE = {
    "database_url": "postgresql+psycopg://agentpay:pw@postgres:5432/agentpay",
    "temporal_address": "temporal:7233",
    "otel_exporter_endpoint": "http://otel-collector:4318",
}


def example() -> dict[str, str]:
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    pairs = [line.split("=", 1) for line in lines if line and not line.startswith("#")]
    return {name: value for name, value in pairs}


def test_every_variable_is_a_setting_or_read_by_compose() -> None:
    fields = {f"AGENTPAY_{name.upper()}" for name in Settings.model_fields}

    assert set(example()) - COMPOSE_ONLY <= fields
    assert not fields & COMPOSE_ONLY


def test_the_hosted_settings_load_as_a_production_demo_on_the_mock_rail() -> None:
    values = {
        name.removeprefix("AGENTPAY_").lower(): value
        for name, value in example().items()
        if name not in COMPOSE_ONLY
    }

    settings = Settings(_env_file=None, **values, **FROM_COMPOSE)  # type: ignore[call-arg]

    assert settings.env == "production"
    assert (settings.rail, settings.mock_settle_after_seconds) == ("mock", 60)
    assert settings.web_origin.startswith("https://")
