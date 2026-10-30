"""The `step_up_consent` workflow on Temporal's time-skipping test server, with real
Postgres (marker `workflow`; CI runs it as its own step).

The exit criterion for Phase 3: a worker process is killed outright while the workflow
waits, and a fresh worker picks the workflow up from Temporal's history and finishes it,
by signal and by timer. That test runs on Temporal's real dev server in real time. The
rest use the time-skipping test server, where timers (days long, since the test clock is
ahead of real time) are skipped while a test awaits a workflow result."""

import asyncio
import logging
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment

from adapters.memory.workflow import InMemoryWorkflow
from adapters.postgres.store import PostgresStore
from adapters.system_clock import SystemClock
from api.main import create_app
from core.domain.consent import ConsentRequestId
from core.domain.decisions import decision_time
from core.relay import relay_once
from tests.api.conftest import NOW, Api, MovableClock
from tests.api.test_consent_approve import Approvals
from tests.conftest import make_settings
from tests.packs import phase_1_packs, release_test_pack
from workflows.activities import ConsentActivities
from workflows.client import TemporalWorkflow
from workflows.consent import ConsentArgs, StepUpConsent
from workflows.worker import consent_worker

pytestmark = pytest.mark.workflow

REPO = Path(__file__).resolve().parents[2]
EVERYTHING = 100_000


@pytest.fixture
def approvals(migrated_database_url: str, tmp_path: Path) -> Iterator[Approvals]:
    store = PostgresStore(create_engine(migrated_database_url))
    clock = MovableClock(NOW)
    workflow = InMemoryWorkflow()
    settings = make_settings(policy_packs_dir=phase_1_packs(tmp_path))
    app = create_app(settings, store=store, clock=clock, workflow=workflow)
    api = Api(TestClient(app), store, clock, workflow=workflow)
    # Other tests' outbox events share this database: deliver them to a fake first, so
    # a worker's relay here starts only this test's workflows.
    relay_once(store, InMemoryWorkflow(), clock, limit=EVERYTHING)
    yield Approvals(api)
    api.assert_evidence_holds()


def _queue() -> str:
    return f"test-consent-{uuid.uuid4().hex[:8]}"


def _consent(approvals: Approvals, csr: str) -> Any:
    with approvals.api.store.transaction(approvals.api.tenant_of(approvals.tenant)) as tx:
        return tx.find_consent_request(ConsentRequestId(csr))


def _resolution_records(approvals: Approvals) -> list[Any]:
    with approvals.api.store.transaction(approvals.api.tenant_of(approvals.tenant)) as tx:
        rows, _ = tx.read_audit_chain()
    return [r for r in rows if r.kind == "consent_resolution"]


async def _until(handle: WorkflowHandle[Any, Any], event_type: int, seconds: float = 60) -> None:
    """Poll the workflow's history until an event of `event_type` appears."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        try:
            if any([e.event_type == event_type async for e in handle.fetch_history_events()]):
                return
        except RPCError:
            pass  # not started yet
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"no {EventType.Name(event_type)} in {handle.id}'s history")
        await asyncio.sleep(0.2)


def _spawn_worker(env: WorkflowEnvironment, queue: str, database_url: str, log: Path) -> Any:
    """`python -m workflows.worker` as its own process, against the dev server."""
    environ = {
        **os.environ,
        "AGENTPAY_ENV": "test",
        "AGENTPAY_DATABASE_URL": database_url,
        "AGENTPAY_TEMPORAL_ADDRESS": env.client.service_client.config.target_host,
        "AGENTPAY_TEMPORAL_NAMESPACE": env.client.namespace,
        "AGENTPAY_TEMPORAL_TASK_QUEUE": queue,
        "AGENTPAY_OUTBOX_POLL_INTERVAL_MS": "200",
    }
    out = log.open("w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "workflows.worker"],
        cwd=REPO,
        env=environ,
        stdout=out,
        stderr=subprocess.STDOUT,
    )


LIVE_TTL_SECONDS = 15  # long enough to kill the worker mid-wait, short enough to wait out


@pytest.fixture
def live(migrated_database_url: str, tmp_path: Path) -> Iterator[Approvals]:
    """Approvals on real time, for Temporal's real dev server: a Mandate valid now and a
    test pack whose Consent requests expire after LIVE_TTL_SECONDS."""
    store = PostgresStore(create_engine(migrated_database_url))
    clock = MovableClock(decision_time(datetime.now(UTC)))
    workflow = InMemoryWorkflow()
    packs = release_test_pack(
        tmp_path / "packs", "1.0.1", {"consent_ttl_seconds": LIVE_TTL_SECONDS}
    )
    app = create_app(
        make_settings(policy_packs_dir=packs), store=store, clock=clock, workflow=workflow
    )
    api = Api(TestClient(app), store, clock, workflow=workflow, packs=packs)
    relay_once(store, InMemoryWorkflow(), clock, limit=EVERYTHING)
    approvals = Approvals(api)
    now = clock.now()
    approvals.mandate_id = approvals.create(
        valid_from=_utc(now - timedelta(minutes=1)),
        expires_at=_utc(now + timedelta(days=30)),
        consent_evidence={
            "method": "upi_mandate_ref",
            "reference": "UPI-MND-LIVE",
            "captured_at": _utc(now - timedelta(minutes=5)),
        },
    )["mandate_id"]
    yield approvals
    api.assert_evidence_holds()


def _utc(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("ending", ["signal", "timer"])
def test_the_workflow_survives_its_worker_being_killed_mid_wait(
    live: Approvals, migrated_database_url: str, tmp_path: Path, ending: str
) -> None:
    csr = live.step_up()["step_up"]["consent_request_id"]
    store = live.api.store

    async def scenario() -> str:
        # The real dev server, not the time-skipping one: only a real server moves a task
        # off a dead worker's sticky queue (after ~5 s) so another worker can take it.
        async with await WorkflowEnvironment.start_local() as env:
            queue = _queue()
            handle = env.client.get_workflow_handle_for(StepUpConsent.run, csr)
            worker_log = tmp_path / "worker.log"
            process = _spawn_worker(env, queue, migrated_database_url, worker_log)
            try:
                # The worker's relay starts the workflow from the outbox; it notifies,
                # then waits on its expiry timer.
                await _until(handle, EventType.EVENT_TYPE_TIMER_STARTED)
            except TimeoutError:
                process.kill()
                raise AssertionError(worker_log.read_text(encoding="utf-8")) from None
            process.kill()  # outright: no shutdown, no chance to finish anything
            process.wait(timeout=30)

            if ending == "signal":
                await asyncio.to_thread(live.approve, csr)  # stored in Postgres
                await handle.signal(StepUpConsent.resolved)  # the API's nudge
            # else: the expiry timer fires with no worker running

            with ThreadPoolExecutor() as pool:
                async with consent_worker(env.client, queue, store, SystemClock(), pool):
                    return await asyncio.wait_for(handle.result(), timeout=90)

    result = asyncio.run(scenario())

    consent = _consent(live, csr)
    if ending == "signal":
        assert (result, consent.resolution.status) == ("resolved", "approved")
    else:
        assert (result, consent.resolution.status) == ("expired", "expired")
        assert consent.resolution.resolved_via == "timer"
    assert len(_resolution_records(live)) == 1


async def _in_process(approvals: Approvals, body: Any) -> Any:
    """Run `body(env, port)` with an in-process worker on the time-skipping server.
    A handle that wasn't returned by `start_workflow` doesn't skip time by itself, so
    bodies move time on with `env.sleep`."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = _queue()
        port = TemporalWorkflow.for_client(env.client, asyncio.get_running_loop(), queue)
        with ThreadPoolExecutor() as pool:
            async with consent_worker(
                env.client, queue, approvals.api.store, approvals.api.clock, pool
            ):
                return await body(env, port)


def test_a_timer_after_the_request_was_resolved_writes_nothing(approvals: Approvals) -> None:
    decision = approvals.step_up()
    csr, expires_at = decision["step_up"]["consent_request_id"], decision["step_up"]["expires_at"]
    tenant = approvals.api.tenant_of(approvals.tenant)

    async def body(env: WorkflowEnvironment, port: TemporalWorkflow) -> str:
        expiry = datetime.fromisoformat(expires_at)
        await asyncio.to_thread(port.start_consent, tenant, ConsentRequestId(csr), expiry)
        handle = env.client.get_workflow_handle_for(StepUpConsent.run, csr)
        await _until(handle, EventType.EVENT_TYPE_TIMER_STARTED)
        await asyncio.to_thread(approvals.decline, csr)  # no signal reaches Temporal
        approvals.api.clock.advance(timedelta(minutes=10))
        await env.sleep(timedelta(days=7))  # past the expiry timer
        return await handle.result()

    assert asyncio.run(_in_process(approvals, body)) == "already_resolved"
    assert _consent(approvals, csr).resolution.status == "declined"
    assert len(_resolution_records(approvals)) == 1


def test_a_duplicate_start_is_a_no_op_even_after_the_workflow_finished(
    approvals: Approvals,
) -> None:
    decision = approvals.step_up()
    csr = ConsentRequestId(decision["step_up"]["consent_request_id"])
    expiry = datetime.fromisoformat(decision["step_up"]["expires_at"])
    tenant = approvals.api.tenant_of(approvals.tenant)

    async def body(env: WorkflowEnvironment, port: TemporalWorkflow) -> tuple[str, str, str]:
        await asyncio.to_thread(port.start_consent, tenant, csr, expiry)
        handle = env.client.get_workflow_handle_for(StepUpConsent.run, csr)
        first = (await handle.describe()).run_id
        await asyncio.to_thread(port.start_consent, tenant, csr, expiry)
        approvals.api.clock.advance(timedelta(minutes=10))
        await env.sleep(timedelta(days=7))
        await handle.result()
        await asyncio.to_thread(port.start_consent, tenant, csr, expiry)  # after it finished
        return first, (await handle.describe()).run_id, (await handle.describe()).status.name

    first, latest, status = asyncio.run(_in_process(approvals, body))
    assert first == latest
    assert status == "COMPLETED"
    assert len(_resolution_records(approvals)) == 1


def test_each_activity_run_twice_writes_one_record(approvals: Approvals) -> None:
    decision = approvals.step_up()
    args = ConsentArgs(
        tenant_id=approvals.api.tenant_of(approvals.tenant),
        consent_request_id=decision["step_up"]["consent_request_id"],
        expires_at=decision["step_up"]["expires_at"],
    )
    activities = ConsentActivities(approvals.api.store, approvals.api.clock)
    approvals.api.clock.advance(timedelta(minutes=10))

    activities.notify(args)
    activities.notify(args)
    outcomes = [activities.expire(args), activities.expire(args)]

    assert outcomes == ["expired", "already_resolved"]
    assert len(_resolution_records(approvals)) == 1


def test_expiry_before_the_request_is_due_is_refused_for_a_retry(approvals: Approvals) -> None:
    decision = approvals.step_up()
    args = ConsentArgs(
        tenant_id=approvals.api.tenant_of(approvals.tenant),
        consent_request_id=decision["step_up"]["consent_request_id"],
        expires_at=decision["step_up"]["expires_at"],
    )
    activities = ConsentActivities(approvals.api.store, approvals.api.clock)

    with pytest.raises(Exception, match="not due yet"):
        activities.expire(args)
    assert _resolution_records(approvals) == []


def test_the_notify_line_carries_identifiers_only(
    approvals: Approvals, caplog: pytest.LogCaptureFixture
) -> None:
    decision = approvals.step_up()
    args = ConsentArgs(
        tenant_id=approvals.api.tenant_of(approvals.tenant),
        consent_request_id=decision["step_up"]["consent_request_id"],
        expires_at=decision["step_up"]["expires_at"],
    )

    with caplog.at_level(logging.INFO, logger="workflows.activities"):
        ConsentActivities(approvals.api.store, approvals.api.clock).notify(args)

    (record,) = [r for r in caplog.records if getattr(r, "event", None)]
    assert record.event == "consent_request.created"  # type: ignore[attr-defined]
    assert record.consent_request_id == args.consent_request_id  # type: ignore[attr-defined]
    assert record.decision_id == decision["decision_id"]  # type: ignore[attr-defined]
    text = record.getMessage()
    assert "tok_1" not in text  # the Payer's contact_ref
    assert not {k for k in vars(record) if "token" in k or "contact" in k}
