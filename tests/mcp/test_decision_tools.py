"""Contract tests for `request_decision` and `get_decision` (#245): the published input
schema, a successful call, API errors as tool errors, and idempotent replay."""

import logging
from typing import Any

import httpx
import pytest

from core.domain.decisions import AgentPaymentRequest
from guard_mcp.http import api_client
from guard_mcp.server import build_server
from tests.api.conftest import Api
from tests.api.test_decisions import Pay
from tests.mcp.conftest import connect, key_of, mcp_settings, payload, text

pytestmark = pytest.mark.anyio


class Agent(Pay):
    """A Tenant with an active Mandate, and a decide-only key for its Agent's MCP server."""

    def __init__(self, api: Api, name: str = "acme") -> None:
        super().__init__(api, name)
        self.key = key_of(api.new_key(self.tenant_id(), {"decide"}))

    def request(self, idempotency_key: str = "buy-1", **overrides: Any) -> dict[str, Any]:
        return {
            "mandate_id": self.mandate_id,
            "agent_id": self.agent_ids[0],
            "amount": {"minor": 1_250_00, "currency": "INR"},
            "merchant": {"id": "m_1", "name": "FreshMart", "mcc": "5411"},
            "idempotency_key": idempotency_key,
        } | overrides


@pytest.fixture
def agent(api: Api) -> Agent:
    return Agent(api)


def _all_keys(schema: object) -> set[str]:
    if isinstance(schema, dict):
        return set(schema) | {k for v in schema.values() for k in _all_keys(v)}
    if isinstance(schema, list):
        return {k for v in schema for k in _all_keys(v)}
    return set()


# --- schemas ---------------------------------------------------------------------------


async def test_request_decision_takes_the_apis_request_model_and_an_idempotency_key() -> None:
    async with api_client(mcp_settings("apg_test_x")) as http:
        tools = {t.name: t for t in (await _list(http))}

    schema = dict(tools["request_decision"].input_schema)
    properties = dict(schema.pop("properties"))
    assert properties.pop("idempotency_key")["type"] == "string"
    assert "idempotency_key" in schema.pop("required")
    expected = AgentPaymentRequest.model_json_schema()
    assert properties == expected.pop("properties")
    assert set(expected.pop("required")) <= set(tools["request_decision"].input_schema["required"])
    assert schema == expected
    assert "idempotency key" in (tools["request_decision"].description or "").lower()


async def test_get_decision_takes_only_a_decision_id() -> None:
    async with api_client(mcp_settings("apg_test_x")) as http:
        tools = {t.name: t for t in (await _list(http))}

    schema = tools["get_decision"].input_schema
    assert schema["required"] == ["decision_id"]
    assert set(schema["properties"]) == {"decision_id"}


async def test_no_tool_takes_a_tenant_id() -> None:
    async with api_client(mcp_settings("apg_test_x")) as http:
        tools = await _list(http)

    for tool in tools:
        assert not any("tenant" in k.lower() for k in _all_keys(tool.input_schema)), tool.name


async def _list(http: httpx.AsyncClient):  # type: ignore[no-untyped-def]
    from mcp import Client

    async with Client(build_server(http)) as client:
        return (await client.list_tools()).tools


# --- request_decision ------------------------------------------------------------------


async def test_an_allow_returns_the_apis_decision(agent: Agent) -> None:
    async with connect(agent.api, agent.key) as client:
        result = await client.call_tool("request_decision", agent.request())

    assert not result.is_error
    body = payload(result)
    assert body["outcome"] == "allow"
    assert body["decision_id"].startswith("dec_")
    read = agent.api.client.get(f"/v1/decisions/{body['decision_id']}", headers=agent.tenant)
    assert body == read.json()


async def test_a_step_up_tells_the_agent_a_person_must_approve(agent: Agent) -> None:
    async with connect(agent.api, agent.key) as client:
        result = await client.call_tool(
            "request_decision", agent.request(amount={"minor": 6_000_00, "currency": "INR"})
        )

    assert not result.is_error
    body = payload(result)
    assert body["outcome"] == "step_up"
    assert body["step_up"]["consent_request_id"].startswith("csr_")
    note = text(result)
    assert "person must approve" in note
    assert "Don't pay" in note and "get_decision" in note


async def test_a_block_returns_its_reason_codes(agent: Agent) -> None:
    async with connect(agent.api, agent.key) as client:
        result = await client.call_tool(
            "request_decision",
            agent.request(merchant={"id": "m_9", "name": "Casino", "mcc": "7995"}),
        )

    assert not result.is_error
    body = payload(result)
    assert body["outcome"] == "block"
    assert body["reason_codes"]


async def test_retrying_with_the_same_idempotency_key_returns_the_same_decision(
    agent: Agent,
) -> None:
    async with connect(agent.api, agent.key) as client:
        first = payload(await client.call_tool("request_decision", agent.request("buy-7")))
        again = payload(await client.call_tool("request_decision", agent.request("buy-7")))
        other = payload(await client.call_tool("request_decision", agent.request("buy-8")))

    assert again == first
    assert other["decision_id"] != first["decision_id"]
    with agent.api.store.transaction(agent.tenant_id()) as tx:
        rows, _ = tx.read_audit_chain()
    assert len(rows) == 2


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"mandate_id": "mdt_unknown"}, "MANDATE_NOT_FOUND"),
        ({"idempotency_key": None}, "IDEMPOTENCY_KEY_MISSING"),
        ({"amount": {"minor": 1.5, "currency": "INR"}}, "VALIDATION_FAILED"),
    ],
    ids=["unknown-mandate", "no-idempotency-key", "invalid-request"],
)
async def test_an_api_error_is_a_tool_error_with_the_apis_code(
    agent: Agent, change: dict[str, Any], code: str
) -> None:
    arguments = {k: v for k, v in (agent.request() | change).items() if v is not None}
    async with connect(agent.api, agent.key) as client:
        result = await client.call_tool("request_decision", arguments)

    assert result.is_error
    assert text(result).startswith(f"{code}: ")
    assert payload(result)["code"] == code


async def test_reusing_an_idempotency_key_for_another_purchase_is_a_tool_error(
    agent: Agent,
) -> None:
    async with connect(agent.api, agent.key) as client:
        await client.call_tool("request_decision", agent.request("buy-1"))
        result = await client.call_tool(
            "request_decision", agent.request("buy-1", amount={"minor": 99_00, "currency": "INR"})
        )

    assert result.is_error
    assert payload(result)["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_an_unreachable_api_is_a_tool_error(agent: Agent) -> None:
    calls = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:  # the startup guard's key check
            return httpx.Response(200, json={"tenant_id": "t", "scopes": ["decide"]})
        raise httpx.ReadTimeout("timed out", request=request)

    from mcp import Client

    from guard_mcp.server import start

    transport = httpx.MockTransport(flaky)
    async with api_client(mcp_settings(agent.key), transport=transport) as http:
        async with Client(await start(http)) as client:
            result = await client.call_tool("request_decision", agent.request())

    assert result.is_error
    assert text(result).startswith("API_UNREACHABLE: ")


async def test_each_call_is_logged_without_the_key_or_the_request(
    agent: Agent, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="guard_mcp")
    async with connect(agent.api, agent.key) as client:
        await client.call_tool("request_decision", agent.request())
        await client.call_tool("request_decision", agent.request("buy-2", mandate_id="mdt_unknown"))

    lines = [r.getMessage() for r in caplog.records if r.name.startswith("guard_mcp")]
    calls = [line for line in lines if "tool=request_decision" in line]
    assert len(calls) == 2
    assert "status=200" in calls[0] and "outcome=allow" in calls[0] and "latency_ms=" in calls[0]
    assert "status=404" in calls[1]
    logged = "\n".join(lines)
    for secret in (agent.key, "FreshMart", "125000", agent.mandate_id, "buy-1"):
        assert secret not in logged


# --- get_decision ----------------------------------------------------------------------


async def test_get_decision_reads_back_a_decision(agent: Agent) -> None:
    async with connect(agent.api, agent.key) as client:
        made = payload(await client.call_tool("request_decision", agent.request()))
        result = await client.call_tool("get_decision", {"decision_id": made["decision_id"]})

    assert not result.is_error
    read = agent.api.client.get(f"/v1/decisions/{made['decision_id']}", headers=agent.tenant)
    assert payload(result) == read.json()


async def test_another_tenants_decision_is_not_found(agent: Agent) -> None:
    theirs = Agent(agent.api, "other")
    async with connect(agent.api, theirs.key) as client:
        made = payload(await client.call_tool("request_decision", theirs.request()))
    async with connect(agent.api, agent.key) as client:
        result = await client.call_tool("get_decision", {"decision_id": made["decision_id"]})

    assert result.is_error
    assert payload(result)["code"] == "DECISION_NOT_FOUND"


@pytest.mark.parametrize("decision_id", ["dec_unknown", "../api-keys/self"])
async def test_an_unknown_decision_id_is_a_tool_error(agent: Agent, decision_id: str) -> None:
    async with connect(agent.api, agent.key) as client:
        result = await client.call_tool("get_decision", {"decision_id": decision_id})

    assert result.is_error
    assert "key_prefix" not in text(result)
