"""The consent workflow is started from the outbox, never from the hot path (ADR-0009).

A step_up Decision writes a `consent_requested` outbox event in its own transaction. The
relay starts one workflow per event through the Workflow port, keyed by the Consent
request ID, and keeps an event until its start succeeds. A resolution signals the
workflow after it commits, and a failed signal never fails the resolution."""

from datetime import timedelta
from pathlib import Path

import pytest

from adapters.memory.clock import FixedClock
from core.ports import DomainEvent
from core.relay import relay_once
from tests.api.conftest import NOW, Api, tenant_webhooks
from tests.api.test_consent_approve import Approvals, inr
from tests.packs import phase_1_packs


@pytest.fixture
def approvals(api: Api, tmp_path: Path) -> Approvals:
    api.use_packs(phase_1_packs(tmp_path))
    return Approvals(api)


# The Postgres test database is shared by every test in the run, so other tests' events
# wait in the same outbox. Each test looks only at its own Tenant's events.
EVERYTHING = 100_000


def _relay(api: Api) -> None:
    clock = FixedClock(NOW)
    relay_once(api.store, api.workflow, clock, tenant_webhooks(api.store, clock), limit=EVERYTHING)


def _waiting(approvals: Approvals) -> list[DomainEvent]:
    tenant = approvals.api.tenant_of(approvals.tenant)
    return [e for e in approvals.api.store.undelivered(EVERYTHING) if e.tenant_id == tenant]


def test_a_step_up_writes_one_consent_requested_event(approvals: Approvals) -> None:
    decision = approvals.step_up()

    events = _waiting(approvals)

    tenant = approvals.api.tenant_of(approvals.tenant)
    assert sorted((e.tenant_id, e.event_type) for e in events) == [
        (tenant, "consent_request.created"),  # for the Tenant's webhook endpoint
        (tenant, "consent_requested"),
    ]
    events = [e for e in events if e.event_type == "consent_requested"]
    assert events[0].payload == {
        "consent_request_id": decision["step_up"]["consent_request_id"],
        "expires_at": "2026-11-02T09:40:00Z",
    }


def test_allow_and_block_write_no_event(approvals: Approvals) -> None:
    approvals.ask(amount=inr(100_00))
    approvals.ask(amount=inr(20_000_00))

    assert _waiting(approvals) == []


def test_the_relay_starts_one_workflow_per_step_up_and_marks_it_delivered(
    approvals: Approvals,
) -> None:
    first, second = approvals.step_up(), approvals.step_up()

    _relay(approvals.api)

    started = approvals.api.workflow.started
    assert {
        first["step_up"]["consent_request_id"],
        second["step_up"]["consent_request_id"],
    } <= set(started)
    assert _waiting(approvals) == []
    calls = approvals.api.workflow.start_calls
    _relay(approvals.api)  # nothing of this Tenant's left to deliver
    assert approvals.api.workflow.start_calls == calls


def test_the_relay_keeps_events_while_the_workflow_side_is_down(approvals: Approvals) -> None:
    csr = approvals.step_up()["step_up"]["consent_request_id"]
    approvals.api.workflow.fail_starts = True

    _relay(approvals.api)
    _relay(approvals.api)
    assert len(_waiting(approvals)) == 1
    assert csr not in approvals.api.workflow.started

    approvals.api.workflow.fail_starts = False
    _relay(approvals.api)
    assert _waiting(approvals) == []
    assert csr in approvals.api.workflow.started


def test_a_duplicate_start_is_a_no_op(approvals: Approvals) -> None:
    csr = approvals.step_up()["step_up"]["consent_request_id"]
    _relay(approvals.api)
    expires_at = approvals.api.workflow.started[csr][1]

    approvals.api.workflow.start_consent(
        approvals.api.tenant_of(approvals.tenant), csr, expires_at + timedelta(hours=1)
    )

    assert approvals.api.workflow.started[csr][1] == expires_at


@pytest.mark.parametrize("resolve", ["approve", "decline"])
def test_a_resolution_signals_the_workflow_after_it_commits(
    approvals: Approvals, resolve: str
) -> None:
    csr = approvals.step_up()["step_up"]["consent_request_id"]

    getattr(approvals, resolve)(csr)

    assert approvals.api.workflow.signals == [(approvals.api.tenant_of(approvals.tenant), csr)]


@pytest.mark.parametrize("resolve", ["approve", "decline"])
def test_a_failed_signal_never_fails_the_resolution(approvals: Approvals, resolve: str) -> None:
    csr = approvals.step_up()["step_up"]["consent_request_id"]
    approvals.api.workflow.fail_signals = True

    response = getattr(approvals, resolve)(csr)

    assert response.status_code == 200
    assert (
        approvals.list(status=response.json()["status"]).json()["data"][0]["consent_request_id"]
        == csr
    )


def test_a_refused_resolution_sends_no_signal(approvals: Approvals) -> None:
    csr = approvals.step_up()["step_up"]["consent_request_id"]
    approvals.decline(csr)
    approvals.api.workflow.signals.clear()

    assert approvals.decline(csr).status_code == 409
    assert approvals.api.workflow.signals == []
