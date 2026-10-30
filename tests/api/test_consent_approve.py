"""POST /v1/consent-requests/{id}/approve, with the re-check at approval (ADR-0010).

Approval re-runs the engine on the Mandate, Agent and Spent this month as they are when
the Payer approves, plus the Signals recorded with the original Decision. A Payer can
lift a step_up but never a block. Every approved and blocked resolution replays
byte-identical when each test ends (the `api` fixture)."""

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from adapters.memory.workflow import InMemoryWorkflow
from adapters.postgres.store import PostgresStore
from api.main import create_app
from tests.api.conftest import NOW, Api, MovableClock
from tests.api.test_consent_decline import ATTESTATION, Declines
from tests.api.test_tenants_payers_agents import assert_problem
from tests.conftest import make_settings
from tests.packs import phase_1_packs


def inr(minor: int) -> dict[str, Any]:
    return {"minor": minor, "currency": "INR"}


class Approvals(Declines):
    def approve(self, csr: str, body: Any = None, *, headers=None):  # type: ignore[no-untyped-def]
        body = {"attestation": ATTESTATION} if body is None else body
        return self.api.post(f"/v1/consent-requests/{csr}/approve", headers or self.consent, body)

    def step_up_for(self, minor: int) -> str:
        body = self.ask(amount=inr(minor)).json()
        assert body["outcome"] == "step_up", body
        csr: str = body["step_up"]["consent_request_id"]
        return csr

    def spend_to(self, allows: int) -> None:
        """`allows` allowed payments of INR 4,000 (Monthly cap INR 50,000)."""
        for _ in range(allows):
            assert self.ask(amount=inr(4_000_00)).json()["outcome"] == "allow"


@pytest.fixture
def approvals(api: Api, tmp_path: Path) -> Approvals:
    api.use_packs(phase_1_packs(tmp_path))
    return Approvals(api)


def test_an_approval_on_an_unchanged_mandate_is_approved(approvals: Approvals) -> None:
    decision = approvals.step_up()
    csr = decision["step_up"]["consent_request_id"]
    approvals.api.clock.advance(timedelta(minutes=3))

    response = approvals.approve(csr)

    assert response.status_code == 200
    resolution = response.json()
    assert (resolution["status"], resolution["resolved_via"]) == ("approved", "tenant_api")
    assert resolution["reason_codes"] is None
    after = approvals.decision(decision["decision_id"])
    assert after["outcome"] == "step_up"
    assert after["consent"] == {
        "status": "approved",
        "resolved_at": "2026-11-02T09:33:00Z",
        "reason_codes": None,
        "mandate_inactive": False,
    }


def test_the_approval_record_holds_the_fresh_inputs_and_the_original_signals(
    approvals: Approvals,
) -> None:
    decision = approvals.step_up()
    approvals.api.clock.advance(timedelta(minutes=3))

    resolution = approvals.approve(decision["step_up"]["consent_request_id"]).json()

    original = approvals.audit(decision["audit_record_id"]).record
    record = approvals.audit(resolution["audit_record_id"]).record
    assert record.kind == "consent_resolution"
    assert record.resolution.status == "approved"
    assert record.resolution.attestation.reference == "app-session-42"
    assert record.inputs.now == datetime(2026, 11, 2, 9, 33, tzinfo=UTC)
    assert record.inputs.request == original.inputs.request
    assert record.inputs.signals == original.inputs.signals
    assert record.pack == original.pack
    assert record.output.outcome == "step_up"  # what the engine said; the Payer lifted it


@pytest.mark.parametrize(
    ("change", "reason"),
    [("revoke", "MANDATE_REVOKED"), ("disable", "AGENT_DISABLED")],
)
def test_an_approval_after_the_mandate_or_agent_was_stopped_is_blocked(
    approvals: Approvals, change: str, reason: str
) -> None:
    decision = approvals.step_up()
    if change == "revoke":
        path, body = f"/v1/mandates/{approvals.mandate_id}/revoke", {"revoked_by": "payer"}
    else:
        path, body = f"/v1/agents/{approvals.agent_ids[0]}/disable", None
    approvals.api.post(path, approvals.tenant, body)

    resolution = approvals.approve(decision["step_up"]["consent_request_id"]).json()

    assert (resolution["status"], resolution["reason_codes"][0]) == ("blocked", reason)
    consent = approvals.decision(decision["decision_id"])["consent"]
    assert (consent["status"], consent["reason_codes"][0]) == ("blocked", reason)


def test_an_approval_that_would_overspend_the_monthly_cap_is_blocked(
    approvals: Approvals,
) -> None:
    approvals.spend_to(9)  # INR 36,000 of INR 50,000
    first, second = approvals.step_up_for(9_000_00), approvals.step_up_for(9_000_00)

    assert approvals.approve(first).json()["status"] == "approved"  # 45,000
    blocked = approvals.approve(second).json()  # would be 54,000

    assert (blocked["status"], blocked["reason_codes"][0]) == ("blocked", "MONTHLY_CAP_EXCEEDED")


def test_an_approved_step_up_counts_in_spent_this_month(approvals: Approvals) -> None:
    approvals.approve(approvals.step_up_for(6_000_00))

    later = approvals.ask(amount=inr(1_00)).json()

    assert approvals.audit(later["audit_record_id"]).record.inputs.spent_this_month.minor == (
        6_000_00
    )


def test_an_approval_after_midnight_utc_counts_in_the_new_month(api: Api, tmp_path: Path) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    api.clock.at = datetime(2026, 11, 30, 23, 58, tzinfo=UTC)
    approvals = Approvals(api)
    approvals.spend_to(9)  # INR 36,000 in November
    csr = approvals.step_up_for(6_000_00)

    api.clock.at = datetime(2026, 12, 1, 0, 3, tzinfo=UTC)
    resolution = approvals.approve(csr).json()

    record = approvals.audit(resolution["audit_record_id"]).record
    assert record.inputs.spent_this_month.minor == 0  # checked against December
    later = approvals.ask(amount=inr(1_00)).json()
    assert approvals.audit(later["audit_record_id"]).record.inputs.spent_this_month.minor == (
        6_000_00
    )


def test_an_approval_needs_an_attestation(approvals: Approvals) -> None:
    csr = approvals.step_up()["step_up"]["consent_request_id"]

    assert_problem(approvals.approve(csr, {}), 422, "VALIDATION_FAILED")


def test_approval_has_the_same_guards_as_decline(approvals: Approvals) -> None:
    declined = approvals.step_up()["step_up"]["consent_request_id"]
    approvals.decline(declined)
    assert_problem(approvals.approve(declined), 409, "CONSENT_ALREADY_RESOLVED")

    agent_tried = approvals.step_up()["step_up"]["consent_request_id"]
    assert_problem(
        approvals.approve(agent_tried, headers=approvals.decide), 403, "INSUFFICIENT_SCOPE"
    )

    approvals.api.clock.advance(timedelta(minutes=10))
    assert_problem(approvals.approve(agent_tried), 409, "CONSENT_EXPIRED")


def test_parallel_approvals_cannot_overspend_the_monthly_cap(
    migrated_database_url: str, tmp_path: Path
) -> None:
    store = PostgresStore(create_engine(migrated_database_url, pool_size=6))
    clock = MovableClock(NOW)
    settings = make_settings(policy_packs_dir=phase_1_packs(tmp_path))
    workflow = InMemoryWorkflow()
    app = create_app(settings, store=store, clock=clock, workflow=workflow)
    api = Api(TestClient(app), store, clock, workflow=workflow)
    approvals = Approvals(api)
    approvals.spend_to(9)
    pending = [approvals.step_up_for(9_000_00) for _ in range(2)]
    start = threading.Barrier(2)
    statuses: list[str] = []
    lock = threading.Lock()

    def approve(csr: str) -> None:
        start.wait()
        status = approvals.approve(csr).json()["status"]
        with lock:
            statuses.append(status)

    workers = [threading.Thread(target=approve, args=(csr,)) for csr in pending]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)

    assert sorted(statuses) == ["approved", "blocked"]
    api.assert_evidence_holds()
