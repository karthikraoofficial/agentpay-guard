"""MockRail behaviour beyond the shared rail contract: outcomes only a fake can force."""

import pytest

from adapters.memory.rail import MockRail
from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
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


def test_refunds_are_not_built_yet() -> None:
    with pytest.raises(NotImplementedError):
        MockRail().refund(TENANT, PaymentId("pmt_x"), AMOUNT, IdempotencyKey("k"))
