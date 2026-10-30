"""The reconciliation sweep (#82): a plain function, run with MockRail and a movable
Clock, with no Temporal. It finds Payments still scheduled after the age in settings,
asks the rail, and applies the same transition a rail webhook does."""

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from adapters.memory.rail import MockRail
from core.domain.payments import PaymentId
from core.ports import TenantId
from core.reconcile import sweep
from tests.api.conftest import Api
from tests.api.test_payments import CrashesBeforeCharging, Payments
from tests.api.test_rail_webhooks import SECRET, Webhooks, event
from tests.conftest import make_settings
from tests.packs import phase_1_packs

AGE = timedelta(minutes=10)


@pytest.fixture
def payments(api: Api, tmp_path: Path) -> Payments:
    api.use_packs(phase_1_packs(tmp_path))
    return Payments(api)


def paid(payments: Payments) -> dict[str, Any]:
    payment: dict[str, Any] = payments.pay(payments.allow()["decision_id"]).json()
    assert payment["status"] == "scheduled"
    return payment


def settle(payments: Payments, payment: dict[str, Any]) -> None:
    payments.api.rail.settle(payments.tenant_id(), PaymentId(payment["payment_id"]))


def run(api: Api) -> Any:
    return sweep(api.store, api.rail, api.clock, AGE)


def statuses(payments: Payments) -> list[str]:
    return [r.payment.status for r in payments.chain() if r.kind == "payment"]


def test_a_payment_the_rail_captured_is_captured_with_one_audit_record(
    payments: Payments,
) -> None:
    payment = paid(payments)
    settle(payments, payment)
    payments.api.clock.advance(timedelta(minutes=11))

    result = run(payments.api)

    current = payments.payment(payment["payment_id"]).json()
    assert current["status"] == "captured"
    assert statuses(payments) == ["scheduled", "captured"]
    assert payments.chain()[-1].payment.rail_payment_id == payment["rail_payment_id"]
    # Other tests' Payments share the Postgres database, so count only errors here.
    assert result.errors == 0


def test_a_payment_the_rail_failed_is_failed_and_voided(api: Api, tmp_path: Path) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    payments = Payments(api, rail_token=MockRail.token_that_fails())
    payment = paid(payments)
    settle(payments, payment)
    api.clock.advance(timedelta(minutes=11))

    run(api)

    current = payments.payment(payment["payment_id"]).json()
    assert (current["status"], current["failure_code"]) == ("failed", "BAD_REQUEST_ERROR")
    assert payments.chain()[-1].kind == "void"


def test_a_payment_scheduled_five_minutes_ago_is_not_touched(payments: Payments) -> None:
    payment = paid(payments)
    settle(payments, payment)
    payments.api.clock.advance(timedelta(minutes=5))

    run(payments.api)

    assert payments.payment(payment["payment_id"]).json()["status"] == "scheduled"
    assert payments.payment(payment["payment_id"]).json()["rail_payment_id"] is not None


def test_a_payment_still_scheduled_at_the_rail_stays_scheduled(payments: Payments) -> None:
    payment = paid(payments)
    payments.api.clock.advance(timedelta(minutes=11))

    run(payments.api)
    run(payments.api)

    assert payments.payment(payment["payment_id"]).json()["status"] == "scheduled"
    assert statuses(payments) == ["scheduled"]


def test_a_sweep_and_a_webhook_give_one_transition(api: Api, tmp_path: Path) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    hooks = Webhooks(api)
    first, second = hooks.paid(), hooks.paid()
    for payment in (first, second):
        settle(hooks, payment)
    api.clock.advance(timedelta(minutes=11))

    hooks.deliver(event(first), "evt_first")  # webhook, then sweep
    run(api)
    hooks.deliver(event(second), "evt_second")  # sweep, then webhook

    assert [hooks.status(p) for p in (first, second)] == ["captured", "captured"]
    assert statuses(hooks) == ["scheduled", "scheduled", "captured", "captured"]


class FlakyRail(MockRail):
    """A rail whose lookup fails for one Payment."""

    def __init__(self) -> None:
        super().__init__()
        self.broken: PaymentId | None = None

    def get_charge(self, tenant_id: TenantId, payment_id: PaymentId):  # type: ignore[no-untyped-def]
        if payment_id == self.broken:
            raise ConnectionError("rail unreachable")
        return super().get_charge(tenant_id, payment_id)


def test_a_rail_error_leaves_that_payment_scheduled_and_the_sweep_goes_on(
    api: Api, tmp_path: Path
) -> None:
    api.rail = FlakyRail()
    api.use_packs(phase_1_packs(tmp_path))
    payments = Payments(api)
    broken, fine = paid(payments), paid(payments)
    for payment in (broken, fine):
        settle(payments, payment)
    api.rail.broken = PaymentId(broken["payment_id"])
    api.clock.advance(timedelta(minutes=11))

    result = run(api)

    assert payments.payment(broken["payment_id"]).json()["status"] == "scheduled"
    assert payments.payment(fine["payment_id"]).json()["status"] == "captured"
    assert result.errors == 1

    api.rail.broken = None
    run(api)  # retried on the next sweep
    assert payments.payment(broken["payment_id"]).json()["status"] == "captured"


def test_a_payment_the_rail_never_saw_is_failed_and_voided(api: Api, tmp_path: Path) -> None:
    api.rail = CrashesBeforeCharging()
    api.use_packs(phase_1_packs(tmp_path))
    payments = Payments(api)
    decision = payments.allow()
    with pytest.raises(RuntimeError, match="process died"):
        payments.pay(decision["decision_id"], key="pay-1")
    payment_id = payments.chain()[-1].payment.payment_id
    api.clock.advance(timedelta(minutes=11))

    run(api)

    current = payments.payment(payment_id).json()
    assert (current["status"], current["failure_code"]) == ("failed", "RAIL_CHARGE_MISSING")
    assert payments.chain()[-1].kind == "void"
    assert api.rail.charges_made == 0  # never charged late


def test_the_sweep_finds_payments_in_every_tenant(payments: Payments) -> None:
    other = Payments(payments.api, "other")
    mine, theirs = paid(payments), paid(other)
    settle(payments, mine)
    settle(other, theirs)
    payments.api.clock.advance(timedelta(minutes=11))

    run(payments.api)

    assert payments.payment(mine["payment_id"]).json()["status"] == "captured"
    assert other.payment(theirs["payment_id"]).json()["status"] == "captured"


# --- The worker ---------------------------------------------------------------------


class _Stop(Exception):
    pass


def test_the_worker_sweeps_on_its_interval(
    payments: Payments, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workflows import worker

    payment = paid(payments)
    settle(payments, payment)
    payments.api.clock.advance(timedelta(minutes=11))
    waits: list[float] = []

    async def stop_after_one_pass(seconds: float) -> None:
        waits.append(seconds)
        raise _Stop

    monkeypatch.setattr(worker.asyncio, "sleep", stop_after_one_pass)
    settings = make_settings(reconcile_interval_seconds=300, reconcile_after_seconds=600)

    with pytest.raises(_Stop):
        asyncio.run(
            worker.sweep_forever(
                payments.api.store, payments.api.rail, payments.api.clock, settings
            )
        )

    assert payments.payment(payment["payment_id"]).json()["status"] == "captured"
    assert waits == [300]


def test_the_worker_does_not_sweep_the_mock_rail() -> None:
    from workflows import worker

    assert worker.sweep_rail(make_settings(rail="mock")) is None
