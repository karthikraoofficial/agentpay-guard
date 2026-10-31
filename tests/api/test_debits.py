"""The worker's debit step (ADR-0017): a card debit is made once its Pre-debit notice
period ends, after checking again that the Mandate and Agent still allow it."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from adapters.memory.rail import MockRail
from core.debits import debit_due
from core.domain.payments import PaymentId
from core.ports import RailUnavailable
from core.reconcile import sweep
from tests.api.conftest import Api
from tests.api.test_payments import Payments
from tests.api.test_rail_webhooks import SECRET, Webhooks, event
from tests.conftest import make_settings
from tests.packs import phase_1_packs

NOTICE = timedelta(hours=36, minutes=5)
SWEEP_AGE = timedelta(minutes=10)


class CountsDebits(MockRail):
    """MockRail with a notice period, counting debit calls per Payment; one Payment's
    next debit can be told to time out. Per Payment, because the debit step looks across
    Tenants and the Postgres store is shared between tests."""

    def __init__(self, api: Api) -> None:
        super().__init__(notice=NOTICE, clock=api.clock)
        self.calls: list[PaymentId] = []
        self.times_out: set[PaymentId] = set()

    def debit(self, tenant_id, rail_token, amount, payment_id, idempotency_key):  # type: ignore[no-untyped-def]
        self.calls.append(payment_id)
        if payment_id in self.times_out:
            self.times_out.discard(payment_id)
            raise RailUnavailable("mock debit timed out")
        return super().debit(tenant_id, rail_token, amount, payment_id, idempotency_key)

    def debits_asked(self, payment: dict[str, Any]) -> int:
        return self.calls.count(PaymentId(payment["payment_id"]))


@pytest.fixture
def payments(api: Api, tmp_path: Path) -> Payments:
    api.rail = CountsDebits(api)
    api.use_packs(phase_1_packs(tmp_path))
    return Payments(api)


def paid(payments: Payments) -> dict[str, Any]:
    payment: dict[str, Any] = payments.pay(payments.allow()["decision_id"]).json()
    return payment


def step(api: Api) -> None:
    debit_due(api.store, api.rail, api.clock)


def status(payments: Payments, payment: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = payments.payment(payment["payment_id"]).json()
    return body


def test_a_payment_records_when_its_debit_may_be_made(payments: Payments) -> None:
    payment = paid(payments)

    assert payment["status"] == "scheduled"
    assert payment["rail_payment_id"] is None
    assert payment["debit_after"] == (payments.api.clock.now() + NOTICE).isoformat().replace(
        "+00:00", "Z"
    )


def test_a_payment_in_its_notice_period_is_left_alone(payments: Payments) -> None:
    api = payments.api
    payment = paid(payments)
    api.clock.advance(NOTICE - timedelta(seconds=1))

    step(api)
    sweep(api.store, api.rail, api.clock, SWEEP_AGE)

    assert api.rail.debits_asked(payment) == 0
    assert payments.charges(payment["payment_id"]) == 0
    assert status(payments, payment)["status"] == "scheduled"


def test_once_the_notice_ends_it_is_debited_once_and_settles(payments: Payments) -> None:
    api = payments.api
    payment = paid(payments)
    api.clock.advance(NOTICE)

    step(api)
    step(api)

    debited = status(payments, payment)
    assert (debited["status"], debited["rail_payment_id"][:9]) == ("scheduled", "pay_mock_")
    assert api.rail.debits_asked(payment) == 1
    assert payments.charges(payment["payment_id"]) == 1
    api.rail.settle(payments.tenant_id(), PaymentId(payment["payment_id"]))
    sweep(api.store, api.rail, api.clock, SWEEP_AGE)
    assert status(payments, payment)["status"] == "captured"


def test_a_debit_whose_answer_is_lost_is_tried_again_and_made_once(payments: Payments) -> None:
    api = payments.api
    payment = paid(payments)
    api.clock.advance(NOTICE)
    api.rail.times_out.add(PaymentId(payment["payment_id"]))

    step(api)
    assert status(payments, payment)["rail_payment_id"] is None
    step(api)

    assert status(payments, payment)["rail_payment_id"].startswith("pay_mock_")
    assert payments.charges(payment["payment_id"]) == 1


@pytest.mark.parametrize(
    ("stop", "code"),
    [("revoke", "MANDATE_REVOKED"), ("disable", "AGENT_DISABLED")],
)
def test_a_mandate_revoked_or_agent_disabled_in_the_notice_period_stops_the_debit(
    payments: Payments, stop: str, code: str
) -> None:
    api = payments.api
    payment = paid(payments)
    if stop == "revoke":
        api.post(
            f"/v1/mandates/{payments.mandate_id}/revoke", payments.tenant, {"revoked_by": "payer"}
        )
    else:
        api.post(f"/v1/agents/{payments.agent_ids[0]}/disable", payments.tenant)
    api.clock.advance(NOTICE)

    step(api)
    step(api)

    stopped = status(payments, payment)
    assert (stopped["status"], stopped["failure_code"]) == ("failed", code)
    assert api.rail.debits_asked(payment) == 0
    kinds = [r.kind for r in payments.chain()]
    assert kinds.count("void") == 1
    assert [r.payment.status for r in payments.chain() if r.kind == "payment"] == [
        "scheduled",
        "failed",
    ]


def test_a_webhook_and_the_debit_step_give_one_transition(api: Api, tmp_path: Path) -> None:
    api.rail = CountsDebits(api)
    api.use_packs(phase_1_packs(tmp_path))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    payments = Webhooks(api)
    payment = paid(payments)
    api.clock.advance(NOTICE)
    step(api)
    debited = status(payments, payment)

    assert payments.deliver(event(debited)).status_code == 200
    step(api)
    sweep(api.store, api.rail, api.clock, SWEEP_AGE)

    assert status(payments, payment)["status"] == "captured"
    assert api.rail.debits_asked(payment) == 1
    assert [r.payment.status for r in payments.chain() if r.kind == "payment"] == [
        "scheduled",
        "captured",
    ]


def test_a_payment_the_rail_answered_but_cannot_find_is_retried_not_failed(
    payments: Payments,
) -> None:
    api = payments.api
    payment = paid(payments)
    api.rail.forget(payments.tenant_id(), PaymentId(payment["payment_id"]))
    api.clock.advance(SWEEP_AGE + timedelta(minutes=1))

    sweep(api.store, api.rail, api.clock, SWEEP_AGE)

    assert status(payments, payment)["status"] == "scheduled"


def test_without_a_notice_period_the_mock_rail_debits_at_once(api: Api, tmp_path: Path) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    payments = Payments(api)

    payment = paid(payments)

    assert payment["debit_after"] is None
    assert payment["rail_payment_id"].startswith("pay_mock_")
    debit_due(api.store, api.rail, api.clock)
    assert payments.charges(payment["payment_id"]) == 1
