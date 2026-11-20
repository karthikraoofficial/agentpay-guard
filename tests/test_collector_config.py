"""The OTel collector's exporters, each on only when its keys are set: traces to Langfuse
(#133) and metrics to Grafana Cloud (#228).

Compose reads the keys from `--env-file .env` and hands them to the collector by
environment; the collector configs only reference them. These tests run `docker compose
config`, which renders the stack without starting it, so they need Docker but no images.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

INFRA = Path("infra")
COMPOSE = INFRA / "docker-compose.yml"
OVERLAY = "/etc/otelcol/langfuse.yaml"
GRAFANA = "/etc/otelcol/grafana.yaml"
GRAFANA_KEYS = (
    "AGENTPAY_GRAFANA_OTLP_ENDPOINT=https://otlp-gateway-prod-ap-south-1.grafana.net/otlp\n"
    "AGENTPAY_GRAFANA_OTLP_AUTH=MTIzOmdsY190b2tlbg==\n"
)

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="needs Docker")


def collector(tmp_path: Path, env: str) -> dict[str, object]:
    env_file = tmp_path / ".env"
    env_file.write_text(env, encoding="utf-8")
    command = ["docker", "compose", "--env-file", str(env_file), "-f", str(COMPOSE), "config"]
    rendered = subprocess.run(  # noqa: S603 - a fixed command; only the env file is ours
        [*command, "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    service: dict[str, object] = json.loads(rendered.stdout)["services"]["otel-collector"]
    return service


def test_without_langfuse_keys_the_collector_only_prints(tmp_path: Path) -> None:
    service = collector(tmp_path, "AGENTPAY_ENV=local\n")

    assert service["command"] == ["--config=/etc/otelcol/config.yaml"]


def test_with_langfuse_keys_the_collector_also_exports_to_langfuse(tmp_path: Path) -> None:
    service = collector(
        tmp_path,
        "AGENTPAY_LANGFUSE_PUBLIC_KEY=pk-lf-1\nAGENTPAY_LANGFUSE_SECRET_KEY=sk-lf-2\n",
    )

    assert service["command"] == ["--config=/etc/otelcol/config.yaml", f"--config={OVERLAY}"]
    assert service["environment"] == {
        "LANGFUSE_PUBLIC_KEY": "pk-lf-1",
        "LANGFUSE_SECRET_KEY": "sk-lf-2",
        "LANGFUSE_HOST": "https://cloud.langfuse.com",
        "GRAFANA_OTLP_ENDPOINT": "",
        "GRAFANA_OTLP_AUTH": "",
    }


def test_with_grafana_keys_the_collector_also_exports_metrics_to_grafana(tmp_path: Path) -> None:
    service = collector(tmp_path, GRAFANA_KEYS)

    assert service["command"] == ["--config=/etc/otelcol/config.yaml", f"--config={GRAFANA}"]
    environment = service["environment"]
    assert environment["GRAFANA_OTLP_ENDPOINT"] == (  # type: ignore[index]
        "https://otlp-gateway-prod-ap-south-1.grafana.net/otlp"
    )
    assert environment["GRAFANA_OTLP_AUTH"] == "MTIzOmdsY190b2tlbg=="  # type: ignore[index]


def test_with_both_sets_of_keys_both_overlays_load(tmp_path: Path) -> None:
    service = collector(tmp_path, "AGENTPAY_LANGFUSE_PUBLIC_KEY=pk\n" + GRAFANA_KEYS)

    assert service["command"] == [
        "--config=/etc/otelcol/config.yaml",
        f"--config={OVERLAY}",
        f"--config={GRAFANA}",
    ]


def test_grafana_gets_metrics_only_and_its_key_comes_from_the_environment() -> None:
    overlay = yaml.safe_load((INFRA / "otel-collector.grafana.yaml").read_text(encoding="utf-8"))

    assert overlay["service"] == {
        "pipelines": {"metrics": {"exporters": ["debug", "otlp_http/grafana"]}}
    }
    exporter = overlay["exporters"]["otlp_http/grafana"]
    assert exporter["endpoint"] == "${env:GRAFANA_OTLP_ENDPOINT}"
    # A header, not a basicauth extension: the collector replaces lists when it merges
    # configs, so a second overlay's `service.extensions` would drop Langfuse's.
    assert exporter["headers"] == {"Authorization": "Basic ${env:GRAFANA_OTLP_AUTH}"}
    assert "extensions" not in overlay


def test_the_langfuse_host_can_be_changed(tmp_path: Path) -> None:
    service = collector(
        tmp_path,
        "AGENTPAY_LANGFUSE_PUBLIC_KEY=pk\nAGENTPAY_LANGFUSE_HOST=https://us.cloud.langfuse.com\n",
    )

    assert service["environment"]["LANGFUSE_HOST"] == "https://us.cloud.langfuse.com"  # type: ignore[index]


def test_langfuse_gets_traces_only_and_keys_come_from_the_environment() -> None:
    overlay = yaml.safe_load((INFRA / "otel-collector.langfuse.yaml").read_text(encoding="utf-8"))

    pipelines = overlay["service"]["pipelines"]
    assert pipelines == {"traces": {"exporters": ["debug", "otlp_http/langfuse"]}}
    assert overlay["extensions"]["basicauth/langfuse"]["client_auth"] == {
        "username": "${env:LANGFUSE_PUBLIC_KEY}",
        "password": "${env:LANGFUSE_SECRET_KEY}",
    }


def test_ci_validates_the_configs_with_the_collector_compose_runs() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    runs = compose["services"]["otel-collector"]["image"]
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    [validates] = re.findall(r"^\s*image=(\S+)$", ci, flags=re.MULTILINE)

    assert validates == runs
    assert "--config=/cfg/otel-collector.grafana.yaml" in ci  # the Grafana overlay too
