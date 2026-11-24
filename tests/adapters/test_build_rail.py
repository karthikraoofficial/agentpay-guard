"""The rail the API builds from settings (#280): the mock rail takes the mock notice period."""

from adapters.memory.rail import MockRail
from adapters.rails import build_rail
from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
from core.domain.rails import RailToken
from core.ports import IdempotencyKey
from tests.conftest import make_settings

CARD = RailToken(
    rail="mock",
    customer_ref="cust_mock",
    token_ref="token_mock_captures",  # noqa: S106  (a mock token ref, not a password)
    display="visa 1111",
)


def _book(rail: object) -> object:
    assert isinstance(rail, MockRail)
    return rail.schedule_charge(
        TenantId("ten_x"),
        CARD,
        Money(minor=100, currency="INR"),
        PaymentId("pay_x"),
        IdempotencyKey("k"),
    )


def test_the_mock_rail_books_a_notice_period_when_one_is_set() -> None:
    settings = make_settings(mock_notice_seconds=120, mock_settle_after_seconds=60)

    charge = _book(build_rail("mock", settings))

    assert charge.debit_after is not None  # type: ignore[attr-defined]
    assert charge.rail_payment_id is None  # type: ignore[attr-defined]  # not debited yet


def test_without_one_the_mock_rail_debits_at_once() -> None:
    charge = _book(build_rail("mock", make_settings()))

    assert charge.debit_after is None  # type: ignore[attr-defined]
    assert charge.rail_payment_id is not None  # type: ignore[attr-defined]
