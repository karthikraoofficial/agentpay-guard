"""The hosted demo's settings (#139, ADR-0021): `.env.production.example` lists real
settings only, and they load."""

from pathlib import Path

from pydantic import SecretStr

from core.settings import DemoRunnerSettings, Settings

EXAMPLE = Path(".env.production.example")
# Read by docker compose or the infra scripts, never by the app.
COMPOSE_ONLY = {
    "AGENTPAY_API_HOST",
    "AGENTPAY_POSTGRES_PASSWORD",
    "AGENTPAY_LANGFUSE_PUBLIC_KEY",
    "AGENTPAY_LANGFUSE_SECRET_KEY",
    "AGENTPAY_LANGFUSE_HOST",
    "AGENTPAY_GRAFANA_OTLP_ENDPOINT",  # the collector (#228)
    "AGENTPAY_GRAFANA_OTLP_AUTH",
    "AGENTPAY_BACKUP_PAR_URL",  # infra/pg-backup.sh (#200)
    "AGENTPAY_BACKUP_PAR_EXPIRES",
    "AGENTPAY_BACKUP_HEARTBEAT_URL",  # infra/pg-backup.sh (#227)
    "AGENTPAY_RESTORE_CHECK_HEARTBEAT_URL",
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
    names = {*Settings.model_fields, *DemoRunnerSettings.model_fields}
    fields = {f"AGENTPAY_{name.upper()}" for name in names}

    assert set(example()) - COMPOSE_ONLY <= fields
    assert not fields & COMPOSE_ONLY


def test_the_hosted_settings_load_as_a_production_demo_on_the_mock_rail() -> None:
    values = {
        name.removeprefix("AGENTPAY_").lower(): value
        for name, value in example().items()
        if name not in COMPOSE_ONLY
    }
    values = {name: value for name, value in values.items() if name in Settings.model_fields}

    settings = Settings(_env_file=None, **values, **FROM_COMPOSE)  # type: ignore[call-arg]

    assert settings.env == "production"
    assert (settings.rail, settings.mock_settle_after_seconds) == ("mock", 60)
    assert settings.web_origin.startswith("https://")


def test_the_hosted_demo_runner_settings_load_with_test_keys() -> None:
    values = {
        name.removeprefix("AGENTPAY_").lower(): value
        for name, value in example().items()
        if name.removeprefix("AGENTPAY_").lower() in DemoRunnerSettings.model_fields
    }

    runner = DemoRunnerSettings(
        _env_file=None,  # type: ignore[call-arg]
        **values,
        demo_agent_key=SecretStr("apg_test_agent"),
        demo_tenant_key=SecretStr("apg_test_tenant"),
        demo_runner_api_base_url="http://api:8000",  # set by infra/docker-compose.prod.yml
    )

    assert runner.web_origin.startswith("https://")
    assert runner.demo_runner_runs_per_ip_per_minute == 5
