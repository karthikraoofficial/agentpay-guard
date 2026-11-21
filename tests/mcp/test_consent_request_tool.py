"""`get_consent_request` (#246), and proof that an Agent can't clear its own step_up:
no tool resolves a Consent request, the MCP server's decide key is refused by the
approve endpoint, and only the Tenant's consent key, out of band, approves it."""

from typing import Any

import pytest
from mcp import Client

from tests.api.conftest import Api
from tests.api.test_consent_decline import ATTESTATION
from tests.mcp.conftest import connect, payload, text
from tests.mcp.test_decision_tools import Agent

pytestmark = pytest.mark.anyio

STEP_UP_AMOUNT = {"minor": 6_000_00, "currency": "INR"}


@pytest.fixture
def agent(api: Api) -> Agent:
    return Agent(api)


async def _step_up(client: Client, agent: Agent, key: str = "buy-1") -> dict[str, Any]:
    result = await client.call_tool("request_decision", agent.request(key, amount=STEP_UP_AMOUNT))
    body = payload(result)
    assert body["outcome"] == "step_up", body
    return body


async def test_get_consent_request_takes_only_a_consent_request_id(agent: Agent) -> None:
    async with connect(agent.api, agent.key) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    schema = tools["get_consent_request"].input_schema
    assert schema["required"] == ["consent_request_id"]
    assert set(schema["properties"]) == {"consent_request_id"}
    assert "Only the Payer can approve" in (tools["get_consent_request"].description or "")


async def test_a_pending_step_up_reads_as_the_api_reports_it(agent: Agent) -> None:
    async with connect(agent.api, agent.key) as client:
        decision = await _step_up(client, agent)
        csr = decision["step_up"]["consent_request_id"]
        result = await client.call_tool("get_consent_request", {"consent_request_id": csr})

    assert not result.is_error
    body = payload(result)
    assert body["status"] == "pending"
    assert body["decision_id"] == decision["decision_id"]
    read = agent.api.client.get(f"/v1/consent-requests/{csr}", headers=agent.tenant)
    assert body == read.json()


@pytest.mark.parametrize("consent_request_id", ["csr_unknown", "../decisions"])
async def test_an_unknown_consent_request_is_a_tool_error(
    agent: Agent, consent_request_id: str
) -> None:
    async with connect(agent.api, agent.key) as client:
        result = await client.call_tool(
            "get_consent_request", {"consent_request_id": consent_request_id}
        )

    assert result.is_error
    if consent_request_id == "csr_unknown":
        assert text(result).startswith("CONSENT_REQUEST_NOT_FOUND: ")


async def test_another_tenants_consent_request_is_not_found(agent: Agent) -> None:
    theirs = Agent(agent.api, "other")
    async with connect(agent.api, theirs.key) as client:
        csr = (await _step_up(client, theirs))["step_up"]["consent_request_id"]
    async with connect(agent.api, agent.key) as client:
        result = await client.call_tool("get_consent_request", {"consent_request_id": csr})

    assert result.is_error
    assert payload(result)["code"] == "CONSENT_REQUEST_NOT_FOUND"


async def test_an_agent_cannot_clear_its_own_step_up(agent: Agent) -> None:
    api = agent.api
    async with connect(api, agent.key) as client:
        decision = await _step_up(client, agent)
        csr = decision["step_up"]["consent_request_id"]

        # No tool resolves it: none is published, and an unlisted name is refused.
        names = {t.name for t in (await client.list_tools()).tools}
        assert not {n for n in names if any(w in n for w in ("approve", "decline", "link"))}
        for name in ("approve_consent_request", "decline_consent_request"):
            refused = await client.call_tool(name, {"consent_request_id": csr})
            assert refused.is_error and text(refused).startswith("UNKNOWN_TOOL: ")

        # The server's own decide key is refused by the approve endpoint itself.
        direct = api.post(
            f"/v1/consent-requests/{csr}/approve",
            {"Authorization": f"Bearer {agent.key}"},
            {"attestation": ATTESTATION},
        )
        assert direct.status_code == 403
        assert direct.json()["code"] == "INSUFFICIENT_SCOPE"
        still = payload(await client.call_tool("get_consent_request", {"consent_request_id": csr}))
        assert still["status"] == "pending"

        # The Tenant's consent key approves it out of band.
        consent_key = api.new_key(agent.tenant_id(), {"consent"})
        approved = api.post(
            f"/v1/consent-requests/{csr}/approve", consent_key, {"attestation": ATTESTATION}
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"

        now = payload(await client.call_tool("get_consent_request", {"consent_request_id": csr}))
        reread = payload(
            await client.call_tool("get_decision", {"decision_id": decision["decision_id"]})
        )

    assert now["status"] == "approved"
    assert reread["consent"]["status"] == "approved"
    assert reread["payable_until"] is not None
