"""The `dispute_pack` workflow on Temporal, with real Postgres (marker `workflow`; CI runs
it as its own step).

Most tests use the time-skipping test server, where the `respond_by` timer (days away)
is skipped while a test awaits the workflow's result. The worker-kill test runs on the
real dev server in real time, as the consent workflow's does."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment

from adapters.memory.workflow import InMemoryWorkflow
from adapters.postgres.store import PostgresStore
from adapters.system_clock import SystemClock
from api.main import create_app
from core.domain.decisions import decision_time
from core.relay import relay_once
from tests.api.conftest import Api, MovableClock, tenant_webhooks
from tests.api.test_disputes import Disputes
from tests.api.test_rail_webhooks import SECRET
from tests.conftest import make_settings
from tests.packs import phase_1_packs
from tests.workflows.test_step_up_consent import (
    EVERYTHING,
    _ahead_of_real_time,
    _queue,
    _spawn_worker,
    _until,
)
from workflows.client import TemporalWorkflow
from workflows.dispute import DisputePack
from workflows.worker import consent_worker

pytestmark = pytest.mark.workflow

Body = Callable[[WorkflowEnvironment, TemporalWorkflow, WorkflowHandle[Any, str]], Awaitable[str]]


def _disputes(database_url: str, tmp_path: Path, clock: MovableClock) -> Disputes:
    store = PostgresStore(create_engine(database_url))
    workflow = InMemoryWorkflow()
    settings = make_settings(
        policy_packs_dir=phase_1_packs(tmp_path), razorpay_webhook_secret=SECRET
    )
    app = create_app(settings, store=store, clock=clock, workflow=workflow)
    api = Api(TestClient(app), store, clock, workflow=workflow)
    # Other tests' outbox events share this database: deliver them to a fake first, so
    # the relay here starts and signals only this test's workflows.
    relay_once(store, InMemoryWorkflow(), clock, tenant_webhooks(store, clock), limit=EVERYTHING)
    return Disputes(api)


@pytest.fixture
def disputes(migrated_database_url: str, tmp_path: Path) -> Iterator[Disputes]:
    made = _disputes(migrated_database_url, tmp_path, MovableClock(_ahead_of_real_time()))
    yield made
    made.api.assert_evidence_holds()


def _relay(disputes: Disputes, port: TemporalWorkflow) -> Awaitable[int]:
    api = disputes.api
    hooks = tenant_webhooks(api.store, api.clock)
    return asyncio.to_thread(relay_once, api.store, port, api.clock, hooks, EVERYTHING)


async def _reaches(disputes: Disputes, dispute_id: str, status: str, version: int) -> None:
    """Poll the API until the Dispute shows `status` with pack `version`."""
    for _ in range(300):
        shown = (await asyncio.to_thread(disputes.get, dispute_id)).json()
        if shown["status"] == status and (shown["pack"] or {}).get("version") == version:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Dispute {dispute_id} never reached {status} v{version}: {shown}")


def _run(disputes: Disputes, dispute_id: str, body: Body) -> str:
    """Run `body` with an in-process worker on the time-skipping server."""
    api = disputes.api

    async def scenario() -> str:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            queue = _queue()
            port = TemporalWorkflow.for_client(env.client, asyncio.get_running_loop(), queue)
            handle = env.client.get_workflow_handle_for(DisputePack.run, dispute_id)
            with ThreadPoolExecutor() as pool:
                async with consent_worker(env.client, queue, api.store, api.clock, pool):
                    return await body(env, port, handle)

    return asyncio.run(scenario())


def test_the_outbox_starts_it_and_a_sign_off_ends_it(disputes: Disputes) -> None:
    dispute_id = disputes.open(disputes.captured()["payment_id"]).json()["dispute_id"]

    async def body(env: WorkflowEnvironment, port: TemporalWorkflow, handle: Any) -> str:
        await _relay(disputes, port)
        # A relay pass that died before marking would start it again: a no-op.
        await asyncio.to_thread(
            port.start_dispute_pack,
            disputes.tenant_id(),
            dispute_id,
            datetime.now(UTC) + timedelta(days=30),
        )
        await _reaches(disputes, dispute_id, "in_review", 1)
        await asyncio.to_thread(disputes.sign_off, dispute_id)
        await _relay(disputes, port)
        result: str = await handle.result()
        return result

    assert _run(disputes, dispute_id, body) == "closed"
    assert disputes.get(dispute_id).json()["status"] == "ready"


def test_a_dispute_nobody_signs_off_expires_at_respond_by(disputes: Disputes) -> None:
    dispute_id = disputes.open(disputes.captured()["payment_id"]).json()["dispute_id"]

    async def body(env: WorkflowEnvironment, port: TemporalWorkflow, handle: Any) -> str:
        await _relay(disputes, port)
        await _reaches(disputes, dispute_id, "in_review", 1)
        await _until(handle, EventType.EVENT_TYPE_TIMER_STARTED)
        disputes.api.clock.advance(timedelta(days=10))
        await env.sleep(timedelta(days=12))  # past the timer, which runs on real time
        result: str = await handle.result()
        return result

    assert _run(disputes, dispute_id, body) == "expired"
    assert disputes.get(dispute_id).json()["status"] == "expired"


def test_a_regenerate_builds_the_next_version_while_it_waits(disputes: Disputes) -> None:
    dispute_id = disputes.open(disputes.captured()["payment_id"]).json()["dispute_id"]

    async def body(env: WorkflowEnvironment, port: TemporalWorkflow, handle: Any) -> str:
        await _relay(disputes, port)
        await _reaches(disputes, dispute_id, "in_review", 1)
        await asyncio.to_thread(disputes.act, dispute_id, "regenerate")
        await _relay(disputes, port)
        await _reaches(disputes, dispute_id, "in_review", 2)
        await asyncio.to_thread(disputes.sign_off, dispute_id, pack_version=2)
        await _relay(disputes, port)
        result: str = await handle.result()
        return result

    assert _run(disputes, dispute_id, body) == "closed"
    shown = disputes.get(dispute_id).json()
    assert (shown["status"], shown["sign_off"]["pack_version"]) == ("ready", 2)


def test_a_dispute_withdrawn_before_its_build_gets_no_pack(disputes: Disputes) -> None:
    dispute_id = disputes.open(disputes.captured()["payment_id"]).json()["dispute_id"]
    disputes.act(dispute_id, "withdraw")

    async def body(env: WorkflowEnvironment, port: TemporalWorkflow, handle: Any) -> str:
        await _relay(disputes, port)  # starts it, and its close signal finds it done or not
        result: str = await handle.result()
        return result

    assert _run(disputes, dispute_id, body) == "closed"
    shown = disputes.get(dispute_id).json()
    assert (shown["status"], shown["pack"]) == ("withdrawn", None)


LIVE_RESPOND_BY_SECONDS = 20  # long enough to kill the worker mid-wait, short to wait out


@pytest.mark.parametrize("ending", ["timer", "regenerate"])
def test_the_workflow_survives_its_worker_being_killed_mid_wait(
    migrated_database_url: str, tmp_path: Path, ending: str
) -> None:
    clock = MovableClock(decision_time(datetime.now(UTC)))
    disputes = _disputes(migrated_database_url, tmp_path, clock)
    payment = disputes.captured()
    respond_by = clock.now() + timedelta(seconds=LIVE_RESPOND_BY_SECONDS)
    dispute_id = disputes.open(payment["payment_id"], respond_by=respond_by.isoformat()).json()[
        "dispute_id"
    ]
    store = disputes.api.store

    async def scenario() -> str:
        # The real dev server: only it moves a task off a dead worker's sticky queue.
        async with await WorkflowEnvironment.start_local() as env:
            queue = _queue()
            handle = env.client.get_workflow_handle_for(DisputePack.run, dispute_id)
            worker_log = tmp_path / "worker.log"
            process = _spawn_worker(env, queue, migrated_database_url, worker_log)
            try:
                # The worker's relay starts it; it builds, then waits on its timer.
                await _until(handle, EventType.EVENT_TYPE_TIMER_STARTED)
            except TimeoutError:
                process.kill()
                raise AssertionError(worker_log.read_text(encoding="utf-8")) from None
            process.kill()  # outright: no shutdown, no chance to finish anything
            process.wait(timeout=30)

            if ending == "regenerate":
                await asyncio.to_thread(disputes.act, dispute_id, "regenerate")  # in Postgres
                await handle.signal(DisputePack.regenerate)  # the relay's nudge
            # either way, the timer then fires with no worker running

            with ThreadPoolExecutor() as pool:
                async with consent_worker(env.client, queue, store, SystemClock(), pool):
                    return await asyncio.wait_for(handle.result(), timeout=90)

    result = asyncio.run(scenario())

    shown = disputes.get(dispute_id).json()
    assert (result, shown["status"]) == ("expired", "expired")
    assert shown["pack"]["version"] == (2 if ending == "regenerate" else 1)
    disputes.api.assert_evidence_holds()
