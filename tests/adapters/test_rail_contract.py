"""The same PaymentRail behaviour from every rail adapter (ADR-0013, ADR-0014).

Each rail joins through a `RailHarness`: the rail itself, Rail tokens that lead to each
outcome, and a way to let a scheduled charge settle. `MockRail` settles on demand; a real
rail joins under `-m contract` and settles by waiting for the rail.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest

from adapters.memory.rail import MockRail
from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId, RailToken
from core.ports import IdempotencyKey, PaymentRail, RailCharge

TENANT = TenantId("ten_rail_contract")
OTHER_TENANT = TenantId("ten_rail_contract_other")
AMOUNT = Money(minor=49_900, currency="INR")


@dataclass(frozen=True)
class RailHarness:
    rail: PaymentRail
    captures: RailToken  # a token whose debit succeeds
    fails: RailToken  # a token the rail accepts, then fails to debit
    settle: Callable[[TenantId, PaymentId], None]


@pytest.fixture(params=["mock"])
def harness(request: pytest.FixtureRequest) -> Iterator[RailHarness]:
    rail = MockRail()
    yield RailHarness(
        rail=rail,
        captures=MockRail.token_that_captures(),
        fails=MockRail.token_that_fails(),
        settle=rail.settle,
    )


def _schedule(h: RailHarness, payment_id: str, token: RailToken | None = None) -> RailCharge:
    return h.rail.schedule_charge(
        TENANT,
        token or h.captures,
        AMOUNT,
        PaymentId(payment_id),
        IdempotencyKey(f"idem_{payment_id}"),
    )


def test_a_charge_is_scheduled_for_exactly_the_amount(harness: RailHarness) -> None:
    charge = _schedule(harness, "pmt_scheduled")

    assert charge.status == "scheduled"
    assert charge.payment_id == "pmt_scheduled"
    assert charge.amount == AMOUNT
    assert charge.failure_code is None


def test_a_scheduled_charge_settles_to_captured(harness: RailHarness) -> None:
    _schedule(harness, "pmt_captures")

    harness.settle(TENANT, PaymentId("pmt_captures"))

    charge = harness.rail.get_charge(TENANT, PaymentId("pmt_captures"))
    assert charge is not None
    assert charge.status == "captured"
    assert charge.amount == AMOUNT
    assert charge.rail_payment_id


def test_scheduling_the_same_payment_again_returns_the_same_charge(harness: RailHarness) -> None:
    first = _schedule(harness, "pmt_twice")

    again = _schedule(harness, "pmt_twice")

    assert again == first
    assert again.rail_payment_id == first.rail_payment_id


def test_scheduling_a_settled_payment_again_never_reopens_it(harness: RailHarness) -> None:
    _schedule(harness, "pmt_settled_twice")
    harness.settle(TENANT, PaymentId("pmt_settled_twice"))

    again = _schedule(harness, "pmt_settled_twice")

    assert again.status == "captured"


def test_a_debit_the_rail_cannot_make_settles_to_failed_with_a_code(harness: RailHarness) -> None:
    _schedule(harness, "pmt_fails", harness.fails)

    harness.settle(TENANT, PaymentId("pmt_fails"))

    charge = harness.rail.get_charge(TENANT, PaymentId("pmt_fails"))
    assert charge is not None
    assert charge.status == "failed"
    assert charge.failure_code


def test_a_payment_the_rail_never_saw_has_no_charge(harness: RailHarness) -> None:
    assert harness.rail.get_charge(TENANT, PaymentId("pmt_never_scheduled")) is None


def test_another_tenant_cannot_see_the_charge(harness: RailHarness) -> None:
    _schedule(harness, "pmt_mine")

    assert harness.rail.get_charge(OTHER_TENANT, PaymentId("pmt_mine")) is None
