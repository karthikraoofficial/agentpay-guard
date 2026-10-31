"""Phase 4 end to end: from an allow to a captured Payment, in one go.

A real test-mode card debit can't complete, because Razorpay waits 36 h 5 min after the
Pre-debit notice and a test token expires before that (ADR-0017). So this runs the whole
path on the mock rail with the same notice period and a clock the test moves. The real
Razorpay half (allow, `scheduled`, notice delivered) is the runbook in the README."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from adapters.memory.rail import MockRail
from api.admin import verify_chain
from core.debits import run_debit_step
from core.domain.payments import PaymentId
from core.reconcile import sweep
from tests.api.conftest import Api
from tests.api.test_rail_webhooks import SECRET, Webhooks, event
from tests.api.test_tenant_webhooks import Hooks, verifies
from tests.conftest import make_settings
from tests.packs import phase_1_packs

NOTICE = timedelta(hours=36, minutes=5)
RECONCILE_AFTER = timedelta(minutes=10)


class Tenant(Hooks, Webhooks):
    """A Tenant with a Mandate whose Rail token captures, a registered webhook endpoint,
    and a way to send it signed Razorpay events."""


@pytest.fixture
def tenant(api: Api, tmp_path: Path) -> Tenant:
    api.rail = MockRail(notice=NOTICE, clock=api.clock)
    api.use_packs(phase_1_packs(tmp_path))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    return Tenant(api)


def debit_step(api: Api) -> None:
    run_debit_step(api.store, api.rail, api.clock)


@pytest.mark.parametrize("settled_by", ["rail webhook", "reconciliation sweep"])
def test_an_allow_becomes_a_captured_payment_the_tenant_hears_about(
    tenant: Tenant, settled_by: str
) -> None:
    api = tenant.api
    secret = tenant.register().json()["secret"]

    decision = tenant.allow()
    paid = tenant.pay(decision["decision_id"])
    payment: dict[str, Any] = paid.json()
    assert paid.status_code == 202
    assert (payment["status"], payment["decision_id"]) == ("scheduled", decision["decision_id"])
    assert payment["amount"] == {"minor": 1_250_00, "currency": "INR"}  # the Decision's

    # The notice period: nothing is debited, however often the worker runs.
    api.clock.advance(NOTICE - timedelta(minutes=1))
    debit_step(api)
    assert tenant.charges(payment["payment_id"]) == 0

    # Once it ends, the worker debits the card exactly once.
    api.clock.advance(timedelta(minutes=1))
    debit_step(api)
    debit_step(api)
    debited: dict[str, Any] = tenant.payment(payment["payment_id"]).json()
    assert debited["status"] == "scheduled"
    assert debited["rail_payment_id"] is not None
    assert tenant.charges(payment["payment_id"]) == 1

    # The rail says it captured: by its webhook, or the sweep asks it.
    api.rail.settle(tenant.tenant_id(), PaymentId(payment["payment_id"]))
    if settled_by == "rail webhook":
        assert tenant.deliver(event(debited), "evt_e2e").status_code == 200
    else:
        api.clock.advance(RECONCILE_AFTER)
        sweep(api.store, api.rail, api.clock, RECONCILE_AFTER)
    captured: dict[str, Any] = tenant.payment(payment["payment_id"]).json()
    assert captured["status"] == "captured"
    assert captured["rail_payment_id"] == debited["rail_payment_id"]

    # The Tenant hears about it through its signed webhook.
    tenant.relay()
    [(body, raw, headers)] = [r for r in tenant.received() if r[0]["type"] == "payment.captured"]
    assert verifies(raw, headers["X-AgentPay-Signature"], secret)
    assert body["data"]["payment_id"] == payment["payment_id"]

    # And the evidence holds: the Decision and each Payment status are in the chain.
    chain = tenant.chain()
    assert next(r.kind for r in chain if r.decision_id == decision["decision_id"]) == "decision"
    statuses = [
        r.payment.status
        for r in chain
        if r.payment is not None and r.payment.payment_id == payment["payment_id"]
    ]
    assert statuses[0] == "scheduled" and statuses[-1] == "captured"
    messages: list[str] = []
    assert verify_chain(api.store, tenant.tenant_id(), messages.append) == 0, messages
