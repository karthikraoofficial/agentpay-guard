"""The same PaymentRail behaviour from every rail adapter (ADR-0013, ADR-0014).

Each rail joins through a `RailHarness`: the rail itself, Rail tokens that lead to each
outcome, and a way to let a scheduled charge settle.

- `mock`: MockRail, which debits at once and settles on demand.
- `mock-notice`: MockRail with a Pre-debit notice period, on a clock the test moves.
- `razorpay-fake`: RazorpayRail against the fake Razorpay API, which also settles on demand.
- `razorpay`: RazorpayRail against the real test account, under `-m contract` only. It needs
  the refs `python -m adapters.razorpay.wizard` prints. It cannot settle: a test token
  expires before Razorpay allows the debit (ADR-0017), so debit and settle cases run on
  the others; it does check that a debit made too early is "not yet".
"""

import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from adapters.memory.rail import MockRail
from adapters.razorpay.rail import RazorpayRail
from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
from core.domain.rails import RAIL_NOTICE_FAILED, RailToken
from core.ports import IdempotencyKey, PaymentRail, RailCharge
from core.settings import get_settings
from tests.adapters.fake_razorpay import NOTICE_SECONDS, FakeRazorpay
from tests.api.conftest import MovableClock

TENANT = TenantId("ten_rail_contract")
OTHER_TENANT = TenantId("ten_rail_contract_other")
AMOUNT = Money(minor=49_900, currency="INR")
WIZARD = "uv run python -m adapters.razorpay.wizard"


@dataclass(frozen=True)
class RailHarness:
    rail: PaymentRail
    captures: RailToken  # a token whose debit succeeds
    refused: RailToken  # a token the rail refuses outright
    fails: RailToken | None  # a token the rail accepts, then fails to debit
    settle: Callable[[TenantId, PaymentId], None] | None  # None: this rail can't settle here
    charges_for: Callable[[TenantId, PaymentId], int]  # Rail charges ever made for a Payment
    run: str  # makes Payment ids unique on a rail that keeps them between runs
    # Moves past every Pre-debit notice period; None when the rail has none (debits at
    # once) or the test can't wait one out (Razorpay).
    end_notice: Callable[[], None] | None = None
    has_notice: bool = False
    # A token whose Pre-debit notice the rail marks failed; None when it can't be forced.
    notice_fails: RailToken | None = None

    def payment_id(self, name: str) -> PaymentId:
        return PaymentId(f"{name}{self.run}")


def _mock() -> RailHarness:
    rail = MockRail()
    return RailHarness(
        rail=rail,
        captures=MockRail.token_that_captures(),
        refused=MockRail.token_that_is_refused(),
        fails=MockRail.token_that_fails(),
        settle=rail.settle,
        charges_for=rail.charges_for,
        run="",
    )


def _mock_notice() -> RailHarness:
    clock = MovableClock(datetime(2026, 10, 31, 6, 0, tzinfo=UTC))
    notice = timedelta(seconds=NOTICE_SECONDS)
    rail = MockRail(notice=notice, clock=clock)

    def settle(tenant_id: TenantId, payment_id: PaymentId) -> None:
        clock.advance(notice)
        token = rail.token_that_captures()  # the token is only read when scheduling
        rail.debit(tenant_id, token, AMOUNT, payment_id, IdempotencyKey("settle"))
        rail.settle(tenant_id, payment_id)

    return RailHarness(
        rail=rail,
        captures=MockRail.token_that_captures(),
        refused=MockRail.token_that_is_refused(),
        fails=MockRail.token_that_fails(),
        settle=settle,
        charges_for=rail.bookings_for,
        run="",
        end_notice=lambda: clock.advance(notice),
        has_notice=True,
        notice_fails=MockRail.token_whose_notice_fails(),
    )


def _razorpay_fake() -> RailHarness:
    captures = RailToken(
        rail="razorpay",
        customer_ref="cust_fake",
        token_ref="token_fake",  # noqa: S106
        display="visa •••• 4366",
    )
    fails = captures.model_copy(update={"token_ref": "token_fake_fails"})
    notice_fails = captures.model_copy(update={"token_ref": "token_fake_notice_fails"})
    fake = FakeRazorpay(
        tokens={captures.token_ref, fails.token_ref, notice_fails.token_ref},
        failed_notices={notice_fails.token_ref},
    )
    rail = RazorpayRail("rzp_test_contract", SecretStr("fake"), transport=fake.transport())

    def end_notice() -> None:
        fake.now += NOTICE_SECONDS + 60

    def settle(tenant_id: TenantId, payment_id: PaymentId) -> None:
        order = next(o for o in fake.orders if o["receipt"] == payment_id)
        token = captures.model_copy(update={"token_ref": order["notification"]["token_id"]})
        end_notice()
        rail.debit(tenant_id, token, AMOUNT, payment_id, IdempotencyKey("settle"))
        if order["notification"]["token_id"] == fails.token_ref:
            fake.settle(payment_id, "failed", "BAD_REQUEST_ERROR")
        else:
            fake.settle(payment_id)

    return RailHarness(
        rail=rail,
        captures=captures,
        refused=captures.model_copy(update={"token_ref": "token_unknown"}),
        fails=fails,
        settle=settle,
        charges_for=lambda _t, payment_id: fake.orders_with(payment_id),
        run="",
        end_notice=end_notice,
        has_notice=True,
        notice_fails=notice_fails,
    )


def _razorpay() -> RailHarness:
    settings = get_settings()
    customer, token = settings.razorpay_test_customer_ref, settings.razorpay_test_token_ref
    keys = (settings.razorpay_key_id, settings.razorpay_key_secret)
    if None in keys or customer is None or token is None:
        pytest.skip(
            "no Razorpay test keys or test token: set the keys and run "
            f"`{WIZARD}`, then put its refs in .env"
        )
    rail = RazorpayRail.from_settings(settings)
    captures = RailToken(
        rail="razorpay", customer_ref=customer, token_ref=token, display="card for contract"
    )
    expires = rail.token_expires_at(captures)
    if expires is None or expires <= datetime.now(UTC):
        pytest.skip(f"the Razorpay test token expired at {expires}: re-run `{WIZARD}`")
    return RailHarness(
        rail=rail,
        captures=captures,
        refused=captures.model_copy(update={"token_ref": "token_doesnotexist01"}),
        fails=None,
        settle=None,
        charges_for=lambda _t, payment_id: rail.orders_for(payment_id),
        run=f"_{secrets.token_hex(4)}",
        has_notice=True,
    )


@pytest.fixture(
    params=[
        "mock",
        "mock-notice",
        "razorpay-fake",
        pytest.param("razorpay", marks=pytest.mark.contract),
    ]
)
def harness(request: pytest.FixtureRequest) -> Iterator[RailHarness]:
    harnesses = {
        "mock": _mock,
        "mock-notice": _mock_notice,
        "razorpay-fake": _razorpay_fake,
        "razorpay": _razorpay,
    }
    yield harnesses[request.param]()


def _settle(h: RailHarness, payment_id: PaymentId) -> None:
    if h.settle is None:
        pytest.skip("a test token expires before Razorpay allows the debit (ADR-0017)")
    h.settle(TENANT, payment_id)


def _seen(h: RailHarness, payment_id: PaymentId) -> RailCharge:
    """The charge once the rail can see it. Nothing asks a rail about a Payment again
    sooner than its lookup lag (ADR-0014); Razorpay's order search takes about 25 s."""
    deadline = time.monotonic() + h.rail.lookup_lag.total_seconds()
    while (charge := h.rail.get_charge(TENANT, payment_id)) is None:
        assert time.monotonic() < deadline, f"the rail never showed {payment_id}"
        time.sleep(3)
    return charge


def _schedule(h: RailHarness, payment_id: PaymentId, token: RailToken | None = None) -> RailCharge:
    return h.rail.schedule_charge(
        TENANT, token or h.captures, AMOUNT, payment_id, IdempotencyKey(f"idem_{payment_id}")
    )


def test_a_charge_is_scheduled_for_exactly_the_amount(harness: RailHarness) -> None:
    payment_id = harness.payment_id("pmt_scheduled")

    charge = _schedule(harness, payment_id)

    assert charge.status == "scheduled"
    assert charge.payment_id == payment_id
    assert charge.amount == AMOUNT
    assert charge.failure_code is None


def test_a_scheduled_charge_can_be_looked_up(harness: RailHarness) -> None:
    payment_id = harness.payment_id("pmt_looked_up")
    scheduled = _schedule(harness, payment_id)

    assert _seen(harness, payment_id) == scheduled


def test_a_scheduled_charge_settles_to_captured(harness: RailHarness) -> None:
    payment_id = harness.payment_id("pmt_captures")
    _schedule(harness, payment_id)

    _settle(harness, payment_id)

    charge = harness.rail.get_charge(TENANT, payment_id)
    assert charge is not None
    assert charge.status == "captured"
    assert charge.amount == AMOUNT
    assert charge.rail_payment_id


def test_scheduling_the_same_payment_again_returns_the_same_charge(harness: RailHarness) -> None:
    payment_id = harness.payment_id("pmt_twice")
    first = _schedule(harness, payment_id)
    _seen(harness, payment_id)

    again = _schedule(harness, payment_id)

    assert again == first
    assert again.rail_payment_id == first.rail_payment_id
    assert harness.charges_for(TENANT, payment_id) == 1


def test_scheduling_a_settled_payment_again_never_reopens_it(harness: RailHarness) -> None:
    payment_id = harness.payment_id("pmt_settled_twice")
    _schedule(harness, payment_id)
    _settle(harness, payment_id)

    again = _schedule(harness, payment_id)

    assert again.status == "captured"


def test_a_debit_the_rail_cannot_make_settles_to_failed_with_a_code(harness: RailHarness) -> None:
    if harness.fails is None:
        pytest.skip("a test token expires before Razorpay allows the debit (ADR-0017)")
    payment_id = harness.payment_id("pmt_fails")
    _schedule(harness, payment_id, harness.fails)

    _settle(harness, payment_id)

    charge = harness.rail.get_charge(TENANT, payment_id)
    assert charge is not None
    assert charge.status == "failed"
    assert charge.failure_code


def test_a_token_the_rail_refuses_fails_at_once_with_a_code(harness: RailHarness) -> None:
    charge = _schedule(harness, harness.payment_id("pmt_refused"), harness.refused)

    assert charge.status == "failed"
    assert charge.failure_code


def test_a_payment_the_rail_never_saw_has_no_charge(harness: RailHarness) -> None:
    assert harness.rail.get_charge(TENANT, harness.payment_id("pmt_never_scheduled")) is None


def test_another_tenant_cannot_see_the_charge(harness: RailHarness) -> None:
    payment_id = harness.payment_id("pmt_mine")
    _schedule(harness, payment_id)
    _seen(harness, payment_id)

    assert harness.rail.get_charge(OTHER_TENANT, payment_id) is None


# --- The debit after a Pre-debit notice (ADR-0017) --------------------------------------


def _debit(h: RailHarness, payment_id: PaymentId) -> RailCharge | None:
    return h.rail.debit(TENANT, h.captures, AMOUNT, payment_id, IdempotencyKey(f"d_{payment_id}"))


def test_a_debit_before_the_notice_period_ends_is_not_yet(harness: RailHarness) -> None:
    if not harness.has_notice:
        pytest.skip("this rail debits when the charge is scheduled")
    payment_id = harness.payment_id("pmt_too_early")
    scheduled = _schedule(harness, payment_id)
    _seen(harness, payment_id)

    charge = _debit(harness, payment_id)

    assert charge is not None
    assert (charge.status, charge.rail_payment_id, charge.failure_code) == ("scheduled", None, None)
    assert scheduled.debit_after is not None
    assert charge.debit_after == scheduled.debit_after


def test_once_the_notice_period_ends_the_debit_is_made_once(harness: RailHarness) -> None:
    if harness.has_notice and harness.end_notice is None:
        pytest.skip("a test token expires before Razorpay allows the debit (ADR-0017)")
    payment_id = harness.payment_id("pmt_debited")
    _schedule(harness, payment_id)
    if harness.end_notice is not None:
        harness.end_notice()

    first = _debit(harness, payment_id)
    again = _debit(harness, payment_id)

    assert first is not None and again is not None
    assert first.status == "scheduled"
    assert first.rail_payment_id
    assert again.rail_payment_id == first.rail_payment_id
    assert harness.rail.get_charge(TENANT, payment_id) == first


def test_a_failed_notice_fails_the_charge_and_no_debit_is_made(harness: RailHarness) -> None:
    if harness.notice_fails is None:
        pytest.skip("this rail has no Pre-debit notice, or can't be made to fail one")
    payment_id = harness.payment_id("pmt_notice_failed")
    scheduled = _schedule(harness, payment_id, harness.notice_fails)
    if harness.end_notice is not None:
        harness.end_notice()

    debited = harness.rail.debit(
        TENANT, harness.notice_fails, AMOUNT, payment_id, IdempotencyKey(f"d_{payment_id}")
    )
    looked_up = harness.rail.get_charge(TENANT, payment_id)

    assert scheduled.status == "scheduled"  # the notice is sent after the order is made
    for charge in (debited, looked_up):
        assert charge is not None
        assert (charge.status, charge.failure_code) == ("failed", RAIL_NOTICE_FAILED)
        assert (charge.rail_payment_id, charge.amount) == (None, AMOUNT)


def test_there_is_nothing_to_debit_for_a_payment_the_rail_never_saw(harness: RailHarness) -> None:
    assert _debit(harness, harness.payment_id("pmt_never_debited")) is None
