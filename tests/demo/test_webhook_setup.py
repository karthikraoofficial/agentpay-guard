"""The provision wizard's webhook step (#286): point the Demo Tenant's Tenant webhook
endpoint at the Demo runner, and hand the wizard the signing secret once."""

import httpx
import pytest

from demo_runner.webhook_setup import SetupFailed, point_endpoint_at
from tests.api.conftest import Api

RUNNER = "https://demo.example/demo-runner/webhooks"


def _client(api: Api, tenant_key: str) -> httpx.Client:
    api.client.headers["Authorization"] = f"Bearer {tenant_key}"
    return api.client  # type: ignore[return-value]


def test_a_new_endpoint_is_registered_and_its_secret_returned(api: Api) -> None:
    tenant_key = api.new_tenant("Demo")["Authorization"].removeprefix("Bearer ")

    secret = point_endpoint_at(_client(api, tenant_key), RUNNER)

    assert secret.startswith("whsec_")
    read = api.client.get("/v1/webhook-endpoint").json()
    assert read["url"] == RUNNER and read["secret"] is None


def test_an_existing_endpoint_is_moved_and_its_secret_rotated(api: Api) -> None:
    tenant_key = api.new_tenant("Demo")["Authorization"].removeprefix("Bearer ")
    client = _client(api, tenant_key)
    first = point_endpoint_at(client, "https://old.example/hooks")

    second = point_endpoint_at(client, RUNNER)

    assert second.startswith("whsec_") and second != first
    assert api.client.get("/v1/webhook-endpoint").json()["url"] == RUNNER


def test_a_refusal_says_why_without_the_key(api: Api) -> None:
    client = _client(api, "apg_test_not_a_real_key")

    with pytest.raises(SetupFailed) as failed:
        point_endpoint_at(client, RUNNER)

    assert "401" in str(failed.value)
    assert "apg_test_not_a_real_key" not in str(failed.value)
