"""`check_intent` (#247): an Agent gets an Intent check through MCP and cites it in
`request_decision`. The model call stays inside the API's Intent check; these tests use
the `api` fixture's scripted gateway, so no real model is ever called."""

from pathlib import Path
from typing import Any

import pytest

from core.domain.intents import IntentCreate
from tests.api.conftest import Api
from tests.api.test_intent_decisions import MEDIUM, groceries
from tests.mcp.conftest import connect, payload, text
from tests.mcp.test_decision_tools import Agent
from tests.packs import signals_pack

pytestmark = pytest.mark.anyio


class Shopper(Agent):
    def check(self, idempotency_key: str = "cart-1", **overrides: Any) -> dict[str, Any]:
        return (
            {
                "mandate_id": self.mandate_id,
                "agent_id": self.agent_ids[0],
                "idempotency_key": idempotency_key,
            }
            | groceries()
            | overrides
        )


@pytest.fixture
def shopper(api: Api, tmp_path: Path) -> Shopper:
    # A pack with the Intent signal, where a medium payment needs a matching Intent check.
    api.use_packs(signals_pack(tmp_path))
    return Shopper(api)


async def test_check_intent_takes_the_apis_request_model_and_an_idempotency_key(
    shopper: Shopper,
) -> None:
    async with connect(shopper.api, shopper.key) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    schema = dict(tools["check_intent"].input_schema)
    properties = dict(schema.pop("properties"))
    assert properties.pop("idempotency_key")["type"] == "string"
    required = schema.pop("required")
    assert "idempotency_key" in required
    expected = IntentCreate.model_json_schema()
    assert properties == expected.pop("properties")
    assert set(expected.pop("required")) | {"idempotency_key"} == set(required)
    assert schema == expected


async def test_a_check_returns_only_its_id_and_expiry(shopper: Shopper) -> None:
    async with connect(shopper.api, shopper.key) as client:
        result = await client.call_tool("check_intent", shopper.check())

    assert not result.is_error
    body = payload(result)
    assert set(body) == {"intent_id", "expires_at"}  # never the verdict, which needs manage
    assert body["intent_id"].startswith("int_")
    assert "match" not in text(result).lower()


async def test_a_check_cited_in_request_decision_is_used(shopper: Shopper) -> None:
    async with connect(shopper.api, shopper.key) as client:
        intent_id = payload(await client.call_tool("check_intent", shopper.check()))["intent_id"]
        cited = payload(
            await client.call_tool(
                "request_decision",
                shopper.request(
                    "buy-1", amount={"minor": MEDIUM, "currency": "INR"}, intent_id=intent_id
                ),
            )
        )
        uncited = payload(
            await client.call_tool(
                "request_decision",
                shopper.request("buy-2", amount={"minor": MEDIUM, "currency": "INR"}),
            )
        )

    assert (cited["outcome"], cited["reason_codes"]) == ("allow", [])
    assert (uncited["outcome"], uncited["reason_codes"]) == ("step_up", ["INTENT_MISSING"])
    signals = shopper.audit(cited["audit_record_id"]).record.inputs.signals
    assert (signals.cited_intent_id, signals.intent_resolution) == (intent_id, "used")


async def test_retrying_a_check_with_the_same_key_returns_it_without_a_second_model_call(
    shopper: Shopper,
) -> None:
    async with connect(shopper.api, shopper.key) as client:
        first = payload(await client.call_tool("check_intent", shopper.check("cart-7")))
        again = payload(await client.call_tool("check_intent", shopper.check("cart-7")))
        other = payload(await client.call_tool("check_intent", shopper.check("cart-8")))

    assert again == first
    assert other["intent_id"] != first["intent_id"]
    assert len(shopper.api.gateway.calls) == 2


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"mandate_id": "mdt_unknown"}, "MANDATE_NOT_FOUND"),
        ({"agent_id": "agt_unknown"}, "AGENT_NOT_FOUND"),
        ({"idempotency_key": None}, "IDEMPOTENCY_KEY_MISSING"),
        ({"payer_instruction": ""}, "VALIDATION_FAILED"),
        ({"tenant_id": "ten_someone_else"}, "VALIDATION_FAILED"),
    ],
    ids=["unknown-mandate", "unknown-agent", "no-idempotency-key", "invalid", "extra-field"],
)
async def test_an_api_error_is_a_tool_error_with_the_apis_code(
    shopper: Shopper, change: dict[str, Any], code: str
) -> None:
    arguments = {k: v for k, v in (shopper.check() | change).items() if v is not None}
    async with connect(shopper.api, shopper.key) as client:
        result = await client.call_tool("check_intent", arguments)

    assert result.is_error
    assert text(result).startswith(f"{code}: ")
    assert shopper.api.gateway.calls == []
