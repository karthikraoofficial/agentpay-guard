"""Mock settle (#135): on the hosted demo, the reconciliation sweep captures mock Payments
once they are old enough, through the same transition a rail webhook uses, so demo
journey 3 can dispute a captured Payment without a real rail."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adapters.memory.settling_rail import SettlingMockRail
from core.reconcile import sweep
from tests.api.conftest import Api
from tests.api.test_disputes import DISPUTE_RULES, DisputeHooks
from tests.api.test_payments import CrashesBeforeCharging, Payments
from tests.api.test_rail_webhooks import SECRET
from tests.conftest import make_settings
from tests.packs import phase_1_packs, release_test_pack

AFTER = timedelta(seconds=60)
LATER = AFTER + timedelta(seconds=1)  # the sweep takes Payments older than its age


@pytest.fixture
def hooks(api: Api, tmp_path: Path) -> DisputeHooks:
    api.use_packs(release_test_pack(tmp_path, "1.0.1", {"disputes": DISPUTE_RULES}))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    return DisputeHooks(api)


def settle(api: Api) -> None:
    sweep(api.store, SettlingMockRail(api.store), api.clock, AFTER)


def scheduled(hooks: DisputeHooks) -> dict[str, Any]:
    payment: dict[str, Any] = hooks.pay(hooks.allow()["decision_id"]).json()
    assert payment["status"] == "scheduled"
    assert payment["rail_payment_id"] is not None  # the mock rail debits at once
    return payment


def statuses(hooks: DisputeHooks, payment_id: str) -> list[str]:
    return [
        r.payment.status
        for r in hooks.chain()
        if r.payment is not None and r.payment.payment_id == payment_id
    ]


def test_a_mock_payment_is_captured_only_once_older_than_the_setting(
    hooks: DisputeHooks,
) -> None:
    payment = scheduled(hooks)

    hooks.api.clock.advance(AFTER)
    settle(hooks.api)
    assert hooks.status(payment) == "scheduled"

    hooks.api.clock.advance(timedelta(seconds=1))
    settle(hooks.api)
    captured = hooks.payment(payment["payment_id"]).json()
    assert captured["status"] == "captured"
    assert captured["rail_payment_id"] == payment["rail_payment_id"]
    assert statuses(hooks, payment["payment_id"]) == ["scheduled", "captured"]


def test_a_second_sweep_changes_nothing(hooks: DisputeHooks) -> None:
    payment = scheduled(hooks)
    hooks.api.clock.advance(LATER)
    settle(hooks.api)
    chain = len(hooks.chain())

    hooks.api.clock.advance(AFTER)
    settle(hooks.api)

    assert len(hooks.chain()) == chain
    assert hooks.status(payment) == "captured"


def test_a_mock_payment_that_never_reached_the_rail_is_failed_not_captured(
    api: Api, tmp_path: Path
) -> None:
    api.rail = CrashesBeforeCharging()
    api.use_packs(phase_1_packs(tmp_path))
    payments = Payments(api)
    with pytest.raises(RuntimeError, match="process died"):
        payments.pay(payments.allow()["decision_id"], key="pay-1")
    payment_id = payments.chain()[-1].payment.payment_id
    api.clock.advance(LATER)

    settle(api)

    current = payments.payment(payment_id).json()
    assert (current["status"], current["failure_code"]) == ("failed", "RAIL_CHARGE_MISSING")


def test_the_tenant_hears_about_a_mock_capture_as_about_any_other(hooks: DisputeHooks) -> None:
    payment = scheduled(hooks)
    hooks.api.clock.advance(LATER)
    settle(hooks.api)

    hooks.relay()

    bodies = [json.loads(b) for url, b, _ in hooks.sender.sent if url == hooks.url]
    captured = [b for b in bodies if b["type"] == "payment.captured"]
    assert [b["data"]["payment_id"] for b in captured] == [payment["payment_id"]]


def test_a_mock_settled_payment_can_be_disputed(hooks: DisputeHooks) -> None:
    payment = scheduled(hooks)
    hooks.api.clock.advance(LATER)
    settle(hooks.api)

    opened = hooks.open(payment["payment_id"], reason="unauthorised")

    assert opened.status_code == 201, opened.text


def test_mock_settle_is_refused_with_the_razorpay_rail() -> None:
    with pytest.raises(ValidationError, match="AGENTPAY_MOCK_SETTLE_AFTER_SECONDS"):
        make_settings(
            rail="razorpay",
            razorpay_key_id="rzp_test_AbCdEf123456",
            razorpay_key_secret="rzp-secret",  # noqa: S106
            razorpay_webhook_secret="whsec",  # noqa: S106
            mock_settle_after_seconds=60,
        )


def test_the_worker_sweeps_the_mock_rail_only_with_mock_settle(api: Api) -> None:
    from workflows import worker

    off = make_settings(rail="mock")
    on = make_settings(rail="mock", mock_settle_after_seconds=60)

    assert worker.sweep_rail(off, api.store) is None
    assert isinstance(worker.sweep_rail(on, api.store), SettlingMockRail)
    assert worker.sweep_age(on) == timedelta(seconds=60)
    assert worker.sweep_age(off) == timedelta(seconds=off.reconcile_after_seconds)
