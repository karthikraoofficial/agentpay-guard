"""The MCP server starts only with a key that holds exactly `decide` (ADR-0011): a key
that can resolve consent would let a prompt-injected Agent approve its own step_up."""

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from core.domain.parties import ALL_SCOPES, ApiKeyScope
from core.settings import McpSettings
from guard_mcp.__main__ import serve
from guard_mcp.guard import KeyRefused
from guard_mcp.http import api_client
from guard_mcp.server import start
from tests.api.conftest import Api
from tests.mcp.conftest import connect, http_to, key_of, mcp_settings

pytestmark = pytest.mark.anyio

HOW = "api.admin create-key --tenant"


@pytest.mark.parametrize(
    "scopes",
    [{"consent"}, {"manage"}, {"decide", "consent"}, {"decide", "manage"}, ALL_SCOPES],
    ids=["consent", "manage", "decide+consent", "decide+manage", "all-scopes"],
)
async def test_a_key_with_more_than_decide_is_refused(api: Api, scopes: set[ApiKeyScope]) -> None:
    tenant_id = api.tenant_of(api.new_tenant())
    key = key_of(api.new_key(tenant_id, scopes))

    async with http_to(api, key) as http:
        with pytest.raises(KeyRefused) as refused:
            await start(http)

    message = str(refused.value)
    assert HOW in message and "--scopes decide" in message
    assert tenant_id in message
    assert key not in message


async def test_a_key_from_before_scopes_is_refused(api: Api) -> None:
    # Keys made before scopes existed hold all three (migration 0008).
    async with http_to(api, key_of(api.new_tenant())) as http:
        with pytest.raises(KeyRefused, match="--scopes decide"):
            await start(http)


async def test_an_unknown_key_is_refused(api: Api) -> None:
    async with http_to(api, "apg_test_not-a-real-key") as http:
        with pytest.raises(KeyRefused, match="refused AGENTPAY_MCP_API_KEY"):
            await start(http)


async def test_an_unreachable_api_is_refused_without_the_key() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    key = "apg_test_secret-value"
    async with api_client(mcp_settings(key), transport=httpx.MockTransport(down)) as http:
        with pytest.raises(KeyRefused, match="Couldn't reach AgentPay Guard") as refused:
            await start(http)
    assert key not in str(refused.value)


async def test_a_decide_key_is_accepted(api: Api) -> None:
    tenant_id = api.tenant_of(api.new_tenant())
    key = key_of(api.new_key(tenant_id, {"decide"}))

    async with connect(api, key) as client:
        tools = await client.list_tools()

    assert {tool.name for tool in tools.tools} == {
        "request_decision",
        "check_intent",
        "get_decision",
        "get_consent_request",
    }


async def test_the_server_exits_non_zero_when_its_key_is_refused(
    api: Api, capsys: pytest.CaptureFixture[str]
) -> None:
    key = key_of(api.new_tenant())
    transport = httpx.ASGITransport(app=api.client.app)

    code = await serve(mcp_settings(key), transport)

    assert code == 1
    err = capsys.readouterr()
    assert "--scopes decide" in err.err
    assert key not in err.err + err.out
    assert err.out == ""  # stdout is the MCP channel


def test_mcp_settings_need_none_of_the_apis_settings() -> None:
    settings = McpSettings(
        _env_file=None, api_base_url="http://localhost:8000", api_key=SecretStr("apg_test_s")
    )

    assert settings.http_timeout_seconds == 10.0
    assert "apg_test_s" not in repr(settings)


def test_mcp_settings_need_a_key_and_a_base_url() -> None:
    with pytest.raises(ValidationError) as missing:
        McpSettings(_env_file=None)
    assert {e["loc"][0] for e in missing.value.errors()} == {"api_base_url", "api_key"}
