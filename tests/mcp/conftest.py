"""The MCP server's one seam is its tool-call interface. Tests drive it with an
in-process MCP client, and point the server's HTTP client at the real FastAPI app (the
`api` fixture's, on the memory store and on Postgres), so every call runs the real API,
store and policy engine."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp_types import CallToolResult
from pydantic import SecretStr

from core.settings import McpSettings
from guard_mcp.http import api_client
from guard_mcp.server import start
from tests.api.conftest import Api, api  # noqa: F401  (the fixture, for tests here)

BASE_URL = "http://guard.test"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def mcp_settings(key: str) -> McpSettings:
    return McpSettings(_env_file=None, api_base_url=BASE_URL, api_key=SecretStr(key))


def key_of(headers: dict[str, str]) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


@asynccontextmanager
async def http_to(app: Api, key: str) -> AsyncIterator[httpx.AsyncClient]:
    """The server's HTTP client, sending to the test app instead of the network."""
    transport = httpx.ASGITransport(app=app.client.app)
    async with api_client(mcp_settings(key), transport=transport) as http:
        yield http


@asynccontextmanager
async def connect(app: Api, key: str) -> AsyncIterator[Client]:
    """An MCP client session on a server that passed its startup key guard."""
    async with http_to(app, key) as http, Client(await start(http)) as client:
        yield client


def payload(result: CallToolResult) -> dict[str, Any]:
    """The API's JSON body, which a tool returns as structured content."""
    assert isinstance(result.structured_content, dict), result
    return result.structured_content


def text(result: CallToolResult) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.content)
