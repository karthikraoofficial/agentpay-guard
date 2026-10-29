"""Latency budgets: decide() p99 < 5 ms; POST /v1/decisions p95 < 150 ms on local
Postgres. Run with `uv run pytest -m perf -s`; CI runs them as their own step.

The API test sends one request at a time. The test client and the app share one Python
process, so parallel clients here would mostly measure contention for the interpreter,
not the service. Concurrency correctness is covered by the overspend test instead.
"""

import statistics
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from adapters.postgres.store import PostgresStore
from api.main import create_app
from tests.api.conftest import NOW, Api, MovableClock
from tests.api.test_mandates import Setup
from tests.conftest import make_settings
from tests.policy.test_engine import run

pytestmark = pytest.mark.perf


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
    yield Api(TestClient(create_app(make_settings(), store=store, clock=clock)), store, clock)


def test_post_decisions_p95_is_under_150_ms_across_50_mandates_and_5_tenants(api: Api) -> None:
    targets: list[tuple[dict[str, str], dict[str, Any]]] = []
    for t in range(5):
        tenant = Setup(api, f"perf-{t}")
        for _ in range(10):
            mandate_id = tenant.create()["mandate_id"]
            body = {
                "mandate_id": mandate_id,
                "agent_id": tenant.agent_ids[0],
                "amount": {"minor": 1_00, "currency": "INR"},
                "merchant": {"id": "m_1", "name": "FreshMart", "mcc": "5411"},
            }
            targets.append((tenant.tenant, body))
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
    assert _report("POST /v1/decisions", samples)["p95"] < 150
