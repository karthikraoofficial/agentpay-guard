"""The OTel collector's Langfuse exporter (#133): on only when the Langfuse keys are set.

Compose reads the keys from `--env-file .env` and hands them to the collector by
environment; the collector configs only reference them. These tests run `docker compose
config`, which renders the stack without starting it, so they need Docker but no images.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

INFRA = Path("infra")
COMPOSE = INFRA / "docker-compose.yml"
OVERLAY = "/etc/otelcol/langfuse.yaml"

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
    }


def test_the_langfuse_host_can_be_changed(tmp_path: Path) -> None:
    service = collector(
        tmp_path,
        "AGENTPAY_LANGFUSE_PUBLIC_KEY=pk\nAGENTPAY_LANGFUSE_HOST=https://us.cloud.langfuse.com\n",
    )

    assert service["environment"]["LANGFUSE_HOST"] == "https://us.cloud.langfuse.com"  # type: ignore[index]


def test_langfuse_gets_traces_only_and_keys_come_from_the_environment() -> None:
    overlay = yaml.safe_load((INFRA / "otel-collector.langfuse.yaml").read_text(encoding="utf-8"))

    pipelines = overlay["service"]["pipelines"]
    assert pipelines == {"traces": {"exporters": ["debug", "otlphttp/langfuse"]}}
    assert overlay["extensions"]["basicauth/langfuse"]["client_auth"] == {
        "username": "${env:LANGFUSE_PUBLIC_KEY}",
        "password": "${env:LANGFUSE_SECRET_KEY}",
    }
