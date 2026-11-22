"""Scenario and Demo runner tests run against the in-process app (the `api` fixture's, on
the memory store and on Postgres), on the mock rail with mock settle on, with a Demo
Tenant's two keys."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from tests.api.conftest import Api, api  # noqa: F401  (the fixture, for tests here)
from tests.demo.operator import demo_keys

BASE_URL = "http://guard.test"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def keys(api: Api) -> tuple[str, str]:  # noqa: F811  (the imported fixture)
    """The Demo Tenant's agent key (decide) and tenant key (consent, manage)."""
    return demo_keys(api)


def transport_to(app: Api) -> httpx.AsyncBaseTransport:
    """Sends to the test app instead of the network."""
    return httpx.ASGITransport(app=app.client.app)


@asynccontextmanager
async def clients(app: Api, keys: tuple[str, str]) -> AsyncIterator[tuple[httpx.AsyncClient, ...]]:
    """An HTTP client for each key, as the Demo runner holds them."""
    transport = transport_to(app)
    agent_key, tenant_key = keys
    async with (
        httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {agent_key}"},
            transport=transport,
        ) as agent,
        httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {tenant_key}"},
            transport=transport,
        ) as tenant,
    ):
        yield agent, tenant
