"""One deployment, two rails (#300, ADR-0033): each Payment goes to the rail its Mandate's
Rail token names, and its refunds follow it there. The Razorpay side runs against the
fake Razorpay API."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from adapters.memory.rail import MockRail
from adapters.memory.settling_rail import SettlingMockRail
from adapters.razorpay.rail import RazorpayRail
from core.debits import run_debit_step
from core.domain.payments import PaymentId
from core.reconcile import sweep
from tests.adapters.fake_razorpay import FakeRazorpay
from tests.adapters.test_razorpay_rail import KEY_ID, KEY_SECRET, TOKEN
from tests.api.conftest import Api
from tests.api.test_disputes import DISPUTE_RULES, SECRET
from tests.api.test_payments import token
from tests.api.test_rail_webhooks import event
from tests.api.test_refunds import Refunds
from tests.api.test_tenants_payers_agents import assert_problem
from tests.conftest import make_settings
from tests.packs import release_test_pack

BOTH = {
    "rails": ("mock", "razorpay"),
    "razorpay_key_id": KEY_ID,
    "razorpay_key_secret": KEY_SECRET,
    "razorpay_webhook_secret": SECRET,
}


@pytest.fixture
def fake() -> FakeRazorpay:
    return FakeRazorpay(tokens={TOKEN.token_ref})


@pytest.fixture
def tenant(api: Api, tmp_path: Path, fake: FakeRazorpay) -> Refunds:
    api.use_packs(release_test_pack(tmp_path, "1.0.1", {"disputes": DISPUTE_RULES}))
    razorpay = RazorpayRail(KEY_ID, SecretStr(KEY_SECRET), transport=fake.transport())
    api.use_rails(make_settings(**BOTH), {"mock": api.rail, "razorpay": razorpay})
    return Refunds(api)


def _razorpay_payment(tenant: Refunds) -> dict[str, Any]:
    mandate = tenant.create(rail_token=token(TOKEN))["mandate_id"]
    paid = tenant.pay(tenant.allow(mandate_id=mandate)["decision_id"])
    assert paid.status_code == 202, paid.text
    payment: dict[str, Any] = paid.json()
    return payment


def test_each_payment_goes_to_the_rail_its_rail_token_names(
    tenant: Refunds, fake: FakeRazorpay
) -> None:
    mock = tenant.pay(tenant.allow()["decision_id"]).json()
    real = _razorpay_payment(tenant)

    assert mock["rail"] == "mock"
    assert tenant.api.rail.charges_for(tenant.tenant_id(), PaymentId(mock["payment_id"])) == 1
    assert real["rail"] == "razorpay"
    assert real["debit_after"] is not None  # Razorpay's Pre-debit notice (ADR-0017)
    assert [o["receipt"] for o in fake.orders] == [real["payment_id"]]
    assert tenant.api.rail.charges_for(tenant.tenant_id(), PaymentId(real["payment_id"])) == 0


def test_a_refund_goes_to_the_rail_its_payment_is_on(tenant: Refunds, fake: FakeRazorpay) -> None:
    real = _razorpay_payment(tenant)
    rail_payment_id = fake.settle(real["payment_id"])
    tenant.deliver(event({**real, "rail_payment_id": rail_payment_id}), "evt_real")
    mock = tenant.captured()

    for payment in (real, mock):
        made = tenant.refund(payment["payment_id"], amount=payment["amount"])
        assert made.status_code == 201, made.text

    refunded_on_razorpay = [r for r in fake.requests if r.url.path.endswith("/refund")]
    assert len(refunded_on_razorpay) == 1
    assert rail_payment_id in str(refunded_on_razorpay[0].url)


@pytest.fixture
def demo(api: Api, tmp_path: Path, fake: FakeRazorpay) -> Refunds:
    """The Demo Tenant on a deployment that reaches both rails, as the hosted demo will."""
    api.use_packs(release_test_pack(tmp_path, "1.0.1", {"disputes": DISPUTE_RULES}))
    tenant = Refunds(api, "Demo")
    razorpay = RazorpayRail(KEY_ID, SecretStr(KEY_SECRET), transport=fake.transport())
    settings = make_settings(
        **BOTH, demo_controls_tenant_id=tenant.tenant_id(), mock_settle_after_seconds=60
    )
    api.use_rails(settings, {"mock": api.rail, "razorpay": razorpay})
    return tenant


def _control(tenant: Refunds, path: str):  # type: ignore[no-untyped-def]
    return tenant.api.post(f"/v1/demo/{path}", tenant.manage, None)


@pytest.mark.parametrize("switch", ["rail-declining", "lose-payment"])
def test_a_rail_switch_refuses_a_mandate_on_a_real_rail(demo: Refunds, switch: str) -> None:
    """No Demo control may touch a real Rail charge (ADR-0033)."""
    real = demo.create(rail_token=token(TOKEN))["mandate_id"]
    simulated = demo.mandate_id

    assert_problem(_control(demo, f"mandates/{real}/{switch}"), 409, "NOT_MOCK_RAIL")
    assert _control(demo, f"mandates/{simulated}/{switch}").status_code == 200


def test_ending_a_notice_period_refuses_a_payment_on_a_real_rail(demo: Refunds) -> None:
    real = _razorpay_payment(demo)

    assert_problem(
        _control(demo, f"payments/{real['payment_id']}/expire-notice"), 409, "NOT_MOCK_RAIL"
    )


# --- The worker: one debit step and sweep per rail ------------------------------------


def test_the_worker_sweeps_each_rail_it_can_answer_for_at_that_rails_age(api: Api) -> None:
    from workflows import worker

    keys = {k: v for k, v in BOTH.items() if k != "rails"}
    without_settle = make_settings(**BOTH)
    with_settle = make_settings(**BOTH, mock_settle_after_seconds=60)

    # Without mock settle, the worker can't see mock charges, so it leaves them alone.
    assert set(worker.sweep_rails(without_settle, api.store)) == {"razorpay"}
    assert set(worker.sweep_rails(with_settle, api.store)) == {"mock", "razorpay"}
    assert worker.sweep_age(with_settle, "mock") == timedelta(seconds=60)
    assert worker.sweep_age(with_settle, "razorpay") == timedelta(
        seconds=with_settle.reconcile_after_seconds
    )
    assert set(worker.sweep_rails(make_settings(rail="razorpay", **keys), api.store)) == {
        "razorpay"
    }


def test_a_sweep_of_one_rail_never_settles_another_rails_payment(tenant: Refunds) -> None:
    mock = tenant.pay(tenant.allow()["decision_id"]).json()
    tenant.api.clock.advance(timedelta(hours=1))
    captures = SettlingMockRail(tenant.api.store)  # captures any debited mock Payment

    sweep(tenant.api.store, captures, tenant.api.clock, timedelta(minutes=10), "razorpay")

    assert tenant.status(mock) == "scheduled"
    sweep(tenant.api.store, captures, tenant.api.clock, timedelta(minutes=10), "mock")
    assert tenant.status(mock) == "captured"


def test_a_debit_step_of_one_rail_never_debits_another_rails_payment(
    tenant: Refunds, fake: FakeRazorpay
) -> None:
    real = _razorpay_payment(tenant)
    tenant.api.clock.advance(timedelta(hours=37))  # past Razorpay's notice period
    mock_rail = MockRail()

    run_debit_step(tenant.api.store, mock_rail, tenant.api.clock, None, "mock")

    assert mock_rail.charges_for(tenant.tenant_id(), PaymentId(real["payment_id"])) == 0
    assert tenant.status(real) == "scheduled"


def test_a_rail_the_deployment_does_not_reach_is_still_not_payable(
    api: Api, tmp_path: Path
) -> None:
    api.use_packs(release_test_pack(tmp_path, "1.0.1", {"disputes": DISPUTE_RULES}))
    tenant = Refunds(api)  # this deployment reaches the mock rail only
    mandate = tenant.create(rail_token=token(TOKEN))["mandate_id"]

    refused = tenant.pay(tenant.allow(mandate_id=mandate)["decision_id"])

    assert_problem(refused, 409, "MANDATE_NOT_PAYABLE")
