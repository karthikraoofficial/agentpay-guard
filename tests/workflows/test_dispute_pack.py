"""The `dispute_pack` workflow on Temporal's time-skipping test server, with real Postgres
(marker `workflow`; CI runs it as its own step): the outbox starts it once per Dispute,
and it builds the Evidence pack that puts the Dispute in review."""

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from temporalio.testing import WorkflowEnvironment

from adapters.memory.workflow import InMemoryWorkflow
from adapters.postgres.store import PostgresStore
from api.main import create_app
from core.domain.disputes import DisputeId
from core.relay import relay_once
from tests.api.conftest import Api, MovableClock, tenant_webhooks
from tests.api.test_disputes import Disputes
from tests.api.test_rail_webhooks import SECRET
from tests.conftest import make_settings
from tests.packs import phase_1_packs
from tests.workflows.test_step_up_consent import EVERYTHING, _ahead_of_real_time, _queue
from workflows.client import TemporalWorkflow
from workflows.dispute import DisputePack
from workflows.worker import consent_worker

pytestmark = pytest.mark.workflow


@pytest.fixture
def disputes(migrated_database_url: str, tmp_path: Path) -> Iterator[Disputes]:
    store = PostgresStore(create_engine(migrated_database_url))
    clock = MovableClock(_ahead_of_real_time())
    workflow = InMemoryWorkflow()
    settings = make_settings(
        policy_packs_dir=phase_1_packs(tmp_path), razorpay_webhook_secret=SECRET
    )
    app = create_app(settings, store=store, clock=clock, workflow=workflow)
    api = Api(TestClient(app), store, clock, workflow=workflow)
    # Other tests' outbox events share this database: deliver them to a fake first, so
    # the relay here starts only this test's workflows.
    relay_once(store, InMemoryWorkflow(), clock, tenant_webhooks(store, clock), limit=EVERYTHING)
    yield Disputes(api)
    api.assert_evidence_holds()


def test_the_outbox_starts_the_workflow_and_it_puts_the_dispute_in_review(
    disputes: Disputes,
) -> None:
    payment = disputes.captured()
    dispute_id = disputes.open(payment["payment_id"]).json()["dispute_id"]
    api = disputes.api

    async def body() -> str:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            queue = _queue()
            port = TemporalWorkflow.for_client(env.client, asyncio.get_running_loop(), queue)
            with ThreadPoolExecutor() as pool:
                async with consent_worker(env.client, queue, api.store, api.clock, pool):
                    hooks = tenant_webhooks(api.store, api.clock)
                    await asyncio.to_thread(
                        relay_once, api.store, port, api.clock, hooks, EVERYTHING
                    )
                    # A relay pass that died before marking would start it again: a no-op.
                    tenant = disputes.tenant_id()
                    await asyncio.to_thread(port.start_dispute_pack, tenant, DisputeId(dispute_id))
                    handle = env.client.get_workflow_handle_for(DisputePack.run, dispute_id)
                    result: str = await handle.result()
                    return result

    assert asyncio.run(body()) == "built"
    dispute: dict[str, Any] = disputes.get(dispute_id).json()
    assert dispute["status"] == "in_review"
    assert dispute["pack"]["version"] == 1
