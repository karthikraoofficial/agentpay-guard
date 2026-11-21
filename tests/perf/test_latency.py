"""Latency budgets: decide() p99 < 5 ms; POST /v1/decisions p95 < 150 ms on local
Postgres; and `request_decision` through the MCP server p95 < 175 ms, the API's budget
plus 25 ms for the stdio hop (#250). Run with `uv run pytest -m perf -s`; CI runs them as
their own step.

The API test sends one request at a time. The test client and the app share one Python
process, so parallel clients here would mostly measure contention for the interpreter,
not the service. Concurrency correctness is covered by the overspend test instead.
"""

import json
import socket
import statistics
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import AsyncExitStack, contextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from sqlalchemy import create_engine

from adapters.memory.model_gateway import ScriptedGateway
from adapters.postgres.store import PostgresStore
from api.main import create_app
from tests.api.conftest import NOW, Api, MovableClock
from tests.api.test_mandates import Setup
from tests.conftest import make_settings
from tests.mcp.conftest import key_of
from tests.policy.test_engine import run

pytestmark = pytest.mark.perf

ROOT = Path(__file__).resolve().parents[2]


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    cuts = statistics.quantiles(samples_ms, n=100, method="inclusive")
    return {"p50": cuts[49], "p95": cuts[94], "p99": cuts[98]}


def _report(name: str, samples_ms: list[float]) -> dict[str, float]:
    p = _percentiles(samples_ms)
    print(f"\n{name}: n={len(samples_ms)} " + " ".join(f"{k}={v:.2f}ms" for k, v in p.items()))
    return p


def test_decide_p99_is_under_5_ms() -> None:
    for _ in range(200):  # warm up
        run()
    samples = []
    for _ in range(10_000):
        start = time.perf_counter()
        run()
        samples.append((time.perf_counter() - start) * 1000)

    assert _report("decide()", samples)["p99"] < 5


@pytest.fixture
def api(migrated_database_url: str) -> Iterator[Api]:
    store = PostgresStore(create_engine(migrated_database_url, pool_size=8))
    clock = MovableClock(NOW)
    gateway = ScriptedGateway()  # the Intent check's model call is off the decision path
    app = create_app(make_settings(), store=store, clock=clock, gateway=gateway)
    yield Api(TestClient(app), store, clock, gateway=gateway)


def _targets(api: Api) -> list[tuple[dict[str, str], dict[str, Any]]]:
    """50 Mandates across 5 Tenants, each with an Agent payment request that runs every
    Signal lookup, and the Tenant's headers."""
    targets: list[tuple[dict[str, str], dict[str, Any]]] = []
    for t in range(5):
        tenant = Setup(api, f"perf-{t}")
        for _ in range(10):
            mandate_id = tenant.create()["mandate_id"]
            # Medium tier, citing an Intent check, so every Signal lookup runs under
            # default@1.1.0: Velocity, New merchant, the Intent check and Price drift.
            amount = {"minor": 3_000_00, "currency": "INR"}
            merchant = {"id": "m_1", "name": "FreshMart", "mcc": "5411"}
            intent = api.post(
                "/v1/intents",
                tenant.tenant,
                {
                    "mandate_id": mandate_id,
                    "agent_id": tenant.agent_ids[0],
                    "payer_instruction": "Weekly groceries",
                    "merchant": merchant,
                    "line_items": [
                        {"description": "Groceries", "quantity": 1, "unit_amount": amount}
                    ],
                    "quoted_total": amount,
                },
            )
            assert intent.status_code == 201, intent.text
            body = {
                "mandate_id": mandate_id,
                "agent_id": tenant.agent_ids[0],
                "amount": amount,
                "merchant": merchant,
                "intent_id": intent.json()["intent_id"],
            }
            targets.append((tenant.tenant, body))
    return targets


def test_post_decisions_p95_is_under_150_ms_across_50_mandates_and_5_tenants(api: Api) -> None:
    targets = _targets(api)
    samples: list[float] = []
    for i in range(1_000):
        headers, body = targets[i % len(targets)]
        start = time.perf_counter()
        response = api.client.post(
            "/v1/decisions", headers={**headers, "Idempotency-Key": f"perf-{i}"}, json=body
        )
        samples.append((time.perf_counter() - start) * 1000)
        assert response.status_code == 200, response.text

    assert len(samples) == 1_000
    assert _report("POST /v1/decisions (Signals on)", samples)["p95"] < 150


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@contextmanager
def _served(app: Any) -> Iterator[str]:
    """The app on a real local port, on a thread, so the MCP server calls it over HTTP."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the API didn't start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.anyio
async def test_request_decision_through_mcp_p95_is_under_175_ms(api: Api, tmp_path: Path) -> None:
    """The API's 150 ms plus 25 ms for the MCP hop: the real server, `python -m
    guard_mcp`, over stdio, calling the API over HTTP on local Postgres. One server per
    Tenant, since each holds one Tenant's decide key, as a host would run it."""
    targets = _targets(api)
    tenants = list(dict.fromkeys(json.dumps(h, sort_keys=True) for h, _ in targets))
    errlog = (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8")
    samples: list[float] = []
    with _served(api.client.app) as base_url, errlog:
        async with AsyncExitStack() as stack:
            clients: dict[str, Client] = {}
            for headers in tenants:
                key = api.new_key(api.tenant_of(json.loads(headers)), {"decide"})
                server = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "guard_mcp"],
                    cwd=str(ROOT),
                    env=get_default_environment()
                    | {
                        "AGENTPAY_MCP_API_BASE_URL": base_url,
                        "AGENTPAY_MCP_API_KEY": key_of(key),
                    },
                )
                clients[headers] = await stack.enter_async_context(
                    Client(stdio_client(server, errlog=errlog))
                )
            for i in range(1_100):
                headers, body = targets[i % len(targets)]
                client = clients[json.dumps(headers, sort_keys=True)]
                start = time.perf_counter()
                result = await client.call_tool(
                    "request_decision", body | {"idempotency_key": f"mcp-perf-{i}"}
                )
                elapsed = (time.perf_counter() - start) * 1000
                assert not result.is_error, result
                if i >= 100:  # the first 100 warm up each server and connection
                    samples.append(elapsed)

    assert len(samples) == 1_000
    assert _report("request_decision through MCP (stdio + HTTP)", samples)["p95"] < 175
