"""MockRail behaviour beyond the shared rail contract: outcomes only a fake can force."""

import pytest

from adapters.memory.rail import MockRail
from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
from core.domain.refunds import RefundId
from core.ports import IdempotencyKey, RailUnavailable

TENANT = TenantId("ten_mock_rail")
AMOUNT = Money(minor=10_000, currency="INR")


def test_a_timeout_after_the_rail_made_the_charge_leaves_it_findable() -> None:
    rail = MockRail()

    with pytest.raises(RailUnavailable):
        rail.schedule_charge(
            TENANT, MockRail.token_that_times_out(), AMOUNT, PaymentId("pmt_t"), IdempotencyKey("k")
        )

    charge = rail.get_charge(TENANT, PaymentId("pmt_t"))
    assert charge is not None
    assert charge.status == "scheduled"


def test_a_retry_after_a_timeout_returns_the_charge_instead_of_making_another() -> None:
    rail = MockRail()
    token = MockRail.token_that_times_out()
    with pytest.raises(RailUnavailable):
        rail.schedule_charge(TENANT, token, AMOUNT, PaymentId("pmt_r"), IdempotencyKey("k"))

    again = rail.schedule_charge(TENANT, token, AMOUNT, PaymentId("pmt_r"), IdempotencyKey("k"))

    assert again == rail.get_charge(TENANT, PaymentId("pmt_r"))
    assert rail.charges_made == 1


def test_a_rail_refusal_comes_back_failed_without_a_charge_to_settle() -> None:
    rail = MockRail()

    charge = rail.schedule_charge(
        TENANT, MockRail.token_that_is_refused(), AMOUNT, PaymentId("pmt_x"), IdempotencyKey("k")
    )

    assert charge.status == "failed"
    assert charge.failure_code
    assert charge.rail_payment_id is None


def _debited(rail: MockRail, payment_id: str = "pmt_x") -> PaymentId:
    pid = PaymentId(payment_id)
    rail.schedule_charge(TENANT, MockRail.token_that_captures(), AMOUNT, pid, IdempotencyKey("k"))
    return pid


def test_a_refund_of_a_debited_charge_is_processed_at_once() -> None:
    rail = MockRail()
    pid = _debited(rail)

    refund = rail.refund(TENANT, pid, RefundId("rfd_1"), AMOUNT, IdempotencyKey("r1"))

    assert refund.status == "processed"
    assert refund.rail_refund_id
    assert refund.amount == AMOUNT


def test_asking_again_for_the_same_refund_never_refunds_twice() -> None:
    rail = MockRail()
    pid = _debited(rail)
    half = Money(minor=AMOUNT.minor // 2, currency=AMOUNT.currency)
    first = rail.refund(TENANT, pid, RefundId("rfd_1"), half, IdempotencyKey("r1"))

    again = rail.refund(TENANT, pid, RefundId("rfd_1"), half, IdempotencyKey("r1"))
    # Had the repeat refunded again, the charge would be used up and this would fail.
    rest = rail.refund(TENANT, pid, RefundId("rfd_2"), half, IdempotencyKey("r2"))

    assert again == first
    assert rest.status == "processed"
    assert rail.refunds_for(TENANT, pid) == 2  # rfd_1 once, rfd_2 once


def test_refunds_never_return_more_than_the_charge_in_all() -> None:
    rail = MockRail()
    pid = _debited(rail)
    part = Money(minor=AMOUNT.minor - 100, currency=AMOUNT.currency)
    rail.refund(TENANT, pid, RefundId("rfd_1"), part, IdempotencyKey("r1"))

    over = rail.refund(
        TENANT,
        pid,
        RefundId("rfd_2"),
        Money(minor=101, currency=AMOUNT.currency),
        IdempotencyKey("r2"),
    )
    rest = rail.refund(
        TENANT,
        pid,
        RefundId("rfd_3"),
        Money(minor=100, currency=AMOUNT.currency),
        IdempotencyKey("r3"),
    )

    assert (over.status, over.failure_code is not None) == ("failed", True)
    assert rest.status == "processed"


def test_a_refund_of_a_charge_the_rail_never_made_fails() -> None:
    refund = MockRail().refund(
        TENANT, PaymentId("pmt_unknown"), RefundId("rfd_1"), AMOUNT, IdempotencyKey("r1")
    )

    assert refund.status == "failed"
    assert refund.rail_refund_id is None
