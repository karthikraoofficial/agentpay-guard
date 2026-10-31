"""POST /v1/payments: an Agent pays an allowed Decision, or an approved step-up (#79).

The only path to the rail. The Payment row, its idempotency entry and a `payment` Audit
record commit before the rail is called, and the rail is asked by Payment id, so one
Decision can never be charged twice (ADR-0014). Refusals are 409s and never reach the
rail. Every chain verifies and every Decision replays when each test ends."""

import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from adapters.memory.rail import MockRail
from adapters.memory.workflow import InMemoryWorkflow
from adapters.postgres.store import PostgresStore
from api.admin import replay_record
from api.main import create_app
from core.domain.decisions import AuditRecordId
from core.domain.payments import PaymentId
from tests.api.conftest import NOW, Api, MovableClock
from tests.api.test_consent_approve import Approvals, inr
from tests.api.test_mandates import _iso
from tests.api.test_tenants_payers_agents import assert_problem
from tests.conftest import make_settings
from tests.packs import phase_1_packs


def token(rail_token: Any) -> dict[str, str]:
    dumped: dict[str, str] = rail_token.model_dump()
    return dumped


class Payments(Approvals):
    """A Tenant whose Mandate carries a mock Rail token that captures."""

    def __init__(self, api: Api, name: str = "acme", rail_token: Any = None) -> None:
        super().__init__(api, name)
        chosen = MockRail.token_that_captures() if rail_token is None else rail_token
        self.mandate_id = self.create(rail_token=token(chosen))["mandate_id"]

    def pay(self, decision_id: str, *, key: str | None = None, headers=None, **extra: Any):  # type: ignore[no-untyped-def]
        body = {"decision_id": decision_id} | extra
        headers = headers or self.decide
        if key is None:
            return self.api.post("/v1/payments", headers, body)
        return self.api.client.post(
            "/v1/payments", headers={**headers, "Idempotency-Key": key}, json=body
        )

    def allow(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = self.ask(**overrides).json()
        assert body["outcome"] == "allow", body
        return body

    def payment(self, payment_id: str, headers=None):  # type: ignore[no-untyped-def]
        return self.api.client.get(f"/v1/payments/{payment_id}", headers=headers or self.decide)

    def charges(self, payment_id: str) -> int:
        return self.api.rail.charges_for(self.tenant_id(), PaymentId(payment_id))

    def chain(self) -> list[Any]:
        with self.api.store.transaction(self.tenant_id()) as tx:
            rows, _ = tx.read_audit_chain()
        return [self.audit(r.audit_record_id).record for r in rows]


@pytest.fixture
def payments(api: Api, tmp_path: Path) -> Payments:
    api.use_packs(phase_1_packs(tmp_path))
    return Payments(api)


# --- Paying -------------------------------------------------------------------------


def test_paying_an_allow_schedules_one_charge_for_exactly_its_amount(payments: Payments) -> None:
    decision = payments.allow()

    response = payments.pay(decision["decision_id"])

    assert response.status_code == 202
    payment = response.json()
    assert payment["payment_id"].startswith("pmt_")
    assert payment["status"] == "scheduled"
    assert payment["decision_id"] == decision["decision_id"]
    assert payment["mandate_id"] == payments.mandate_id
    assert payment["amount"] == {"minor": 1_250_00, "currency": "INR"}
    assert payment["rail"] == "mock"
    assert payment["rail_payment_id"].startswith("pay_mock_")
    assert payment["failure_code"] is None
    assert payments.charges(payment["payment_id"]) == 1
    tenant = payments.tenant_id()
    charge = payments.api.rail.get_charge(tenant, PaymentId(payment["payment_id"]))
    assert charge is not None and charge.amount.minor == 1_250_00
    assert payments.payment(payment["payment_id"]).json() == payment


def test_the_rail_is_asked_to_charge_the_mandates_rail_token(payments: Payments) -> None:
    other = Payments(payments.api, "other", MockRail.token_that_is_refused())

    payment = other.pay(other.allow()["decision_id"]).json()

    # The refused token is the one on the Mandate, so the rail refused it.
    assert (payment["status"], payment["failure_code"]) == ("failed", "BAD_REQUEST_ERROR")


def test_an_approved_step_up_is_payable_for_the_window_from_its_approval(
    payments: Payments,
) -> None:
    decision = payments.step_up()
    payments.api.clock.advance(timedelta(minutes=9))
    payments.approve(decision["step_up"]["consent_request_id"])
    # 22 minutes after the Decision, 13 after the approval.
    payments.api.clock.advance(timedelta(minutes=13))

    response = payments.pay(decision["decision_id"])

    assert response.status_code == 202, response.json()
    assert response.json()["amount"] == decision_amount(payments, decision)


def decision_amount(payments: Payments, decision: dict[str, Any]) -> dict[str, Any]:
    record = payments.audit(decision["audit_record_id"]).record
    amount: dict[str, Any] = record.inputs.request.amount.model_dump(mode="json")
    return amount


def test_the_agent_cannot_change_the_amount(payments: Payments) -> None:
    decision = payments.allow()

    response = payments.pay(decision["decision_id"], amount=inr(1_00))

    assert response.status_code == 422
    assert payments.api.rail.charges_made == 0


# --- Refusals -----------------------------------------------------------------------


def refused(payments: Payments, decision_id: str, code: str) -> None:
    head = len(payments.chain())
    assert_problem(payments.pay(decision_id), 409, code)
    assert payments.api.rail.charges_made == 0
    assert len(payments.chain()) == head  # refusals are not audited


def test_a_block_is_not_payable(payments: Payments) -> None:
    decision = payments.ask(merchant={"id": "m_9", "name": "Casino", "mcc": "7995"}).json()
    assert decision["outcome"] == "block"

    refused(payments, decision["decision_id"], "DECISION_NOT_PAYABLE")


def test_a_pending_declined_expired_or_blocked_step_up_is_not_payable(
    payments: Payments,
) -> None:
    pending = payments.step_up()
    refused(payments, pending["decision_id"], "DECISION_NOT_PAYABLE")

    declined = payments.step_up()
    payments.decline(declined["step_up"]["consent_request_id"])
    refused(payments, declined["decision_id"], "DECISION_NOT_PAYABLE")

    blocked = payments.step_up()
    payments.api.post(f"/v1/agents/{payments.agent_ids[0]}/disable", payments.tenant)
    payments.approve(blocked["step_up"]["consent_request_id"])
    payments.api.post(f"/v1/agents/{payments.agent_ids[0]}/enable", payments.tenant)
    refused(payments, blocked["decision_id"], "DECISION_NOT_PAYABLE")

    payments.api.clock.advance(timedelta(minutes=11))
    refused(payments, pending["decision_id"], "DECISION_NOT_PAYABLE")  # now expired


def test_a_decision_is_paid_only_once(payments: Payments) -> None:
    decision = payments.allow()
    first = payments.pay(decision["decision_id"]).json()

    assert_problem(payments.pay(decision["decision_id"]), 409, "DECISION_ALREADY_PAID")
    assert payments.charges(first["payment_id"]) == 1
    assert payments.api.rail.charges_made == 1


def test_a_payment_after_the_window_is_refused(payments: Payments) -> None:
    decision = payments.allow()
    payments.api.clock.advance(timedelta(minutes=15))  # the window is 15 minutes, end exclusive

    refused(payments, decision["decision_id"], "PAYMENT_WINDOW_CLOSED")


def test_a_revoked_mandate_or_a_disabled_agent_stops_an_allow_already_given(
    payments: Payments,
) -> None:
    revoked = payments.allow()
    disabled = payments.allow(agent_id=payments.agent_ids[1])

    payments.api.post(f"/v1/agents/{payments.agent_ids[1]}/disable", payments.tenant)
    refused(payments, disabled["decision_id"], "AGENT_DISABLED")

    payments.api.post(
        f"/v1/mandates/{payments.mandate_id}/revoke", payments.tenant, {"revoked_by": "payer"}
    )
    refused(payments, revoked["decision_id"], "MANDATE_REVOKED")


def test_a_mandate_that_expired_since_the_allow_is_refused(payments: Payments) -> None:
    short = payments.create(
        rail_token=token(MockRail.token_that_captures()), expires_at=_iso(timedelta(minutes=5))
    )
    decision = payments.allow(mandate_id=short["mandate_id"])
    payments.api.clock.advance(timedelta(minutes=6))

    refused(payments, decision["decision_id"], "MANDATE_EXPIRED")


def test_a_mandate_without_a_rail_token_is_not_payable(payments: Payments) -> None:
    bare = payments.create()
    decision = payments.allow(mandate_id=bare["mandate_id"])

    refused(payments, decision["decision_id"], "MANDATE_NOT_PAYABLE")


def test_a_rail_token_for_another_rail_is_not_payable(payments: Payments) -> None:
    razorpay = {
        "rail": "razorpay",
        "customer_ref": "cust_Mx1aB2cD3eF4gH",
        "token_ref": "token_Mx1aB2cD3eF4gI",
        "display": "visa 1111",
    }
    elsewhere = payments.create(rail_token=razorpay)  # the configured rail is mock
    decision = payments.allow(mandate_id=elsewhere["mandate_id"])

    refused(payments, decision["decision_id"], "MANDATE_NOT_PAYABLE")


def test_another_tenants_decision_or_payment_is_not_found(payments: Payments) -> None:
    other = Payments(payments.api, "other")
    theirs = other.allow()
    their_payment = other.pay(theirs["decision_id"]).json()

    assert_problem(payments.pay(theirs["decision_id"]), 404, "DECISION_NOT_FOUND")
    assert_problem(payments.pay("dec_unknown"), 404, "DECISION_NOT_FOUND")
    assert_problem(payments.payment(their_payment["payment_id"]), 404, "PAYMENT_NOT_FOUND")
    assert payments.api.rail.charges_made == 1


def test_a_refusal_is_logged_and_traced_with_its_code(
    payments: Payments, caplog: pytest.LogCaptureFixture
) -> None:
    decision = payments.step_up()

    with caplog.at_level("INFO"):
        payments.pay(decision["decision_id"])

    span = payments.api.telemetry.spans[-1]
    assert span.name == "payment"
    assert span.attributes["refusal_code"] == "DECISION_NOT_PAYABLE"
    assert span.attributes["decision_id"] == decision["decision_id"]
    assert any("DECISION_NOT_PAYABLE" in r.getMessage() for r in caplog.records)


# --- Keys and idempotency -----------------------------------------------------------


def test_paying_needs_a_decide_key_and_an_idempotency_key(payments: Payments) -> None:
    decision = payments.allow()

    assert_problem(
        payments.pay(decision["decision_id"], headers=payments.consent), 403, "INSUFFICIENT_SCOPE"
    )
    response = payments.api.client.post(
        "/v1/payments", headers=payments.decide, json={"decision_id": decision["decision_id"]}
    )
    assert response.status_code == 400
    assert payments.api.rail.charges_made == 0


def test_a_retry_with_the_same_key_returns_the_same_response(payments: Payments) -> None:
    decision = payments.allow()
    first = payments.pay(decision["decision_id"], key="pay-1")

    again = payments.pay(decision["decision_id"], key="pay-1")

    assert again.status_code == 202
    assert again.content == first.content
    assert again.headers["Idempotent-Replayed"] == "true"
    assert payments.charges(first.json()["payment_id"]) == 1


# --- Rail outcomes ------------------------------------------------------------------


def test_a_rail_timeout_leaves_the_payment_scheduled_and_a_retry_finds_the_charge(
    api: Api, tmp_path: Path
) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    payments = Payments(api, rail_token=MockRail.token_that_times_out())
    decision = payments.allow()

    first = payments.pay(decision["decision_id"], key="pay-1")

    payment = first.json()
    assert first.status_code == 202
    assert (payment["status"], payment["rail_payment_id"]) == ("scheduled", None)
    # The same key asks the rail again, which finds the charge it already made.
    retried = payments.pay(decision["decision_id"], key="pay-1").json()
    assert retried["payment_id"] == payment["payment_id"]
    assert retried["rail_payment_id"].startswith("pay_mock_")
    assert payments.pay(decision["decision_id"], key="pay-1").json() == retried
    assert_problem(payments.pay(decision["decision_id"]), 409, "DECISION_ALREADY_PAID")
    assert payments.charges(payment["payment_id"]) == 1


class CrashesBeforeCharging(MockRail):
    """A rail the API dies on before the request reaches it, once."""

    def __init__(self) -> None:
        super().__init__()
        self.crashed = False

    def schedule_charge(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("process died before calling the rail")
        return super().schedule_charge(*args, **kwargs)


def test_a_retry_after_a_crash_before_the_rail_call_charges_once(api: Api, tmp_path: Path) -> None:
    api.rail = CrashesBeforeCharging()
    api.use_packs(phase_1_packs(tmp_path))
    payments = Payments(api)
    decision = payments.allow()
    with pytest.raises(RuntimeError, match="process died"):
        payments.pay(decision["decision_id"], key="pay-1")
    assert api.rail.charges_made == 0

    retried = payments.pay(decision["decision_id"], key="pay-1")

    assert retried.status_code == 202
    payment = retried.json()
    assert (payment["status"], payment["rail_payment_id"][:9]) == ("scheduled", "pay_mock_")
    assert payments.charges(payment["payment_id"]) == 1


class LagsTwoMinutes(MockRail):
    """A rail whose lookups may miss a charge for two minutes, as Razorpay's order search
    does, counting every time it is asked to schedule."""

    lookup_lag = timedelta(minutes=2)

    def __init__(self) -> None:
        super().__init__()
        self.asked = 0

    def schedule_charge(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.asked += 1
        return super().schedule_charge(*args, **kwargs)


def test_a_retry_never_asks_a_rail_again_before_its_lookups_catch_up(
    api: Api, tmp_path: Path
) -> None:
    api.rail = LagsTwoMinutes()
    api.use_packs(phase_1_packs(tmp_path))
    payments = Payments(api, rail_token=MockRail.token_that_times_out())
    decision = payments.allow()
    first = payments.pay(decision["decision_id"], key="pay-1").json()

    api.clock.advance(timedelta(seconds=119))
    soon = payments.pay(decision["decision_id"], key="pay-1")
    api.clock.advance(timedelta(seconds=1))
    later = payments.pay(decision["decision_id"], key="pay-1").json()

    assert soon.headers["Idempotent-Replayed"] == "true"
    assert soon.json() == first
    assert api.rail.asked == 2  # the first call, then once the two minutes had passed
    assert later["rail_payment_id"].startswith("pay_mock_")
    assert payments.charges(first["payment_id"]) == 1


def test_a_refused_debit_fails_the_payment_with_the_rails_code(api: Api, tmp_path: Path) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    payments = Payments(api, rail_token=MockRail.token_that_is_refused())
    decision = payments.allow()

    first = payments.pay(decision["decision_id"], key="pay-1")

    payment = first.json()
    assert first.status_code == 202
    assert (payment["status"], payment["failure_code"]) == ("failed", "BAD_REQUEST_ERROR")
    assert payments.payment(payment["payment_id"]).json() == payment
    assert payments.pay(decision["decision_id"], key="pay-1").content == first.content
    statuses = [r.payment.status for r in payments.chain() if r.kind == "payment"]
    assert statuses == ["scheduled", "failed"]


# --- Audit --------------------------------------------------------------------------


def test_a_payment_joins_the_tenants_audit_chain(payments: Payments) -> None:
    decision = payments.allow()

    payment = payments.pay(decision["decision_id"]).json()

    record = payments.chain()[-1]
    assert record.kind == "payment"
    assert record.decision_id == decision["decision_id"]
    assert record.payment.payment_id == payment["payment_id"]
    assert record.payment.status == "scheduled"
    assert record.payment.amount.minor == 1_250_00
    assert record.inputs is None and record.output is None
    sealed = payments.audit(AuditRecordId(record.audit_record_id))
    assert b"token_mock" not in sealed.body  # the Payment record names no instrument
    messages: list[str] = []
    tenant = payments.tenant_id()
    code = replay_record(
        payments.api.store, tenant, record.audit_record_id, payments.api.packs, messages.append
    )
    assert code == 0
    assert "is a scheduled Payment record with no engine run" in messages[0]


# --- Concurrency (Postgres: the in-memory store doesn't model parallel writers) ----------


def test_two_concurrent_payments_of_one_decision_give_one_202_and_one_409(
    migrated_database_url: str, tmp_path: Path
) -> None:
    store = PostgresStore(create_engine(migrated_database_url, pool_size=6))
    clock = MovableClock(NOW)
    settings = make_settings(policy_packs_dir=phase_1_packs(tmp_path))
    workflow, rail = InMemoryWorkflow(), MockRail()
    app = create_app(settings, store=store, clock=clock, workflow=workflow, rail=rail)
    api = Api(TestClient(app), store, clock, workflow=workflow, rail=rail)
    payments = Payments(api)
    decision = payments.allow()
    start = threading.Barrier(2)
    statuses: list[int] = []
    lock = threading.Lock()

    def pay(key: str) -> None:
        start.wait()
        status = payments.pay(decision["decision_id"], key=key).status_code
        with lock:
            statuses.append(status)

    workers = [threading.Thread(target=pay, args=(f"race-{i}",)) for i in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)

    assert sorted(statuses) == [202, 409]
    assert rail.charges_made == 1
    api.assert_evidence_holds()


def test_the_app_charges_through_razorpay_when_it_is_the_rail() -> None:
    from adapters.razorpay.rail import RazorpayRail

    settings = make_settings(
        rail="razorpay",
        razorpay_key_id="rzp_test_AbCdEf123456",
        razorpay_key_secret="rzp-secret",  # noqa: S106
        razorpay_webhook_secret="whsec",  # noqa: S106
    )

    assert isinstance(create_app(settings).state.rail, RazorpayRail)
