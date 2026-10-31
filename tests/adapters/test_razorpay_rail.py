"""RazorpayRail against a fake Razorpay API (tests/adapters/fake_razorpay.py).

The shared rail behaviour is in test_rail_contract.py; these cover what is Razorpay's own:
the order it makes, how its errors map, and what never reaches the logs.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from adapters.razorpay.rail import RazorpayRail
from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
from core.domain.rails import RAIL_NOTICE_FAILED, RailToken
from core.ports import IdempotencyKey, RailUnavailable
from tests.adapters.fake_razorpay import CREATED_AT, NOTICE_SECONDS, FakeRazorpay
from tests.conftest import make_settings

TENANT = TenantId("ten_rzp")
AMOUNT = Money(minor=49_900, currency="INR")
KEY_ID = "rzp_test_FakeKeyId0001"
KEY_SECRET = "fake-secret-never-logged"  # noqa: S105
TOKEN = RailToken(
    rail="razorpay",
    customer_ref="cust_fake001",
    token_ref="token_fake001",  # noqa: S106
    display="visa •••• 4366",
)
UNKNOWN = TOKEN.model_copy(update={"token_ref": "token_doesnotexist01"})


@pytest.fixture
def fake() -> FakeRazorpay:
    return FakeRazorpay(tokens={TOKEN.token_ref})


@pytest.fixture
def rail(fake: FakeRazorpay) -> RazorpayRail:
    return RazorpayRail(KEY_ID, SecretStr(KEY_SECRET), transport=fake.transport())


def _schedule(rail: RazorpayRail, payment_id: str, token: RailToken = TOKEN):  # type: ignore[no-untyped-def]
    return rail.schedule_charge(
        TENANT, token, AMOUNT, PaymentId(payment_id), IdempotencyKey(f"idem_{payment_id}")
    )


def test_scheduling_makes_an_order_with_a_pre_debit_notice(
    rail: RazorpayRail, fake: FakeRazorpay
) -> None:
    charge = _schedule(rail, "pmt_one")

    [order] = fake.orders
    assert order["receipt"] == "pmt_one"
    assert (order["amount"], order["currency"]) == (49_900, "INR")
    assert order["notes"] == {"agentpay_payment_id": "pmt_one", "agentpay_tenant_id": "ten_rzp"}
    assert order["notification"]["token_id"] == "token_fake001"  # noqa: S105
    created = [r for r in fake.requests if r.method == "POST"]
    assert json.loads(created[0].content)["payment_capture"] is True
    assert charge.status == "scheduled"
    assert charge.amount == AMOUNT
    assert charge.rail_payment_id is None  # no Razorpay payment until the debit
    assert charge.debit_after == datetime.fromtimestamp(CREATED_AT + NOTICE_SECONDS, UTC)


def test_scheduling_again_finds_the_order_instead_of_making_another(
    rail: RazorpayRail, fake: FakeRazorpay
) -> None:
    first = _schedule(rail, "pmt_twice")

    again = _schedule(rail, "pmt_twice")

    assert fake.orders_with("pmt_twice") == 1
    assert again == first


def test_a_timeout_is_unavailable_and_the_retry_finds_the_order_it_made(
    rail: RazorpayRail, fake: FakeRazorpay
) -> None:
    fake.times_out_after_creating = True
    with pytest.raises(RailUnavailable):
        _schedule(rail, "pmt_lost_answer")

    charge = _schedule(rail, "pmt_lost_answer")

    assert charge.status == "scheduled"
    assert fake.orders_with("pmt_lost_answer") == 1


@pytest.mark.parametrize("status", [500, 503, 429, 401])
def test_a_server_error_rate_limit_or_bad_key_is_unavailable_not_failed(
    rail: RazorpayRail, fake: FakeRazorpay, status: int
) -> None:
    fake.next_status = status

    with pytest.raises(RailUnavailable):
        _schedule(rail, "pmt_unavailable")

    assert fake.orders == []


def test_a_lookup_that_times_out_is_unavailable(rail: RazorpayRail, fake: FakeRazorpay) -> None:
    fake.next_times_out = True

    with pytest.raises(RailUnavailable):
        rail.get_charge(TENANT, PaymentId("pmt_lookup"))


def test_a_token_razorpay_refuses_fails_the_charge_with_its_code(
    rail: RazorpayRail, fake: FakeRazorpay
) -> None:
    charge = _schedule(rail, "pmt_refused", UNKNOWN)

    assert charge.status == "failed"
    assert charge.failure_code == "BAD_REQUEST_ERROR"
    assert fake.orders == []


def test_a_payment_with_no_order_has_no_charge(rail: RazorpayRail) -> None:
    assert rail.get_charge(TENANT, PaymentId("pmt_never")) is None


def test_another_tenants_order_is_not_found(rail: RazorpayRail) -> None:
    _schedule(rail, "pmt_theirs")

    assert rail.get_charge(TenantId("ten_other"), PaymentId("pmt_theirs")) is None


def test_an_order_in_its_notice_period_is_scheduled_until_its_debit(
    rail: RazorpayRail,
) -> None:
    _schedule(rail, "pmt_waiting")

    charge = rail.get_charge(TENANT, PaymentId("pmt_waiting"))

    assert charge is not None
    assert charge.status == "scheduled"
    assert charge.debit_after == datetime.fromtimestamp(CREATED_AT + NOTICE_SECONDS, UTC)


def test_a_captured_debit_is_captured_with_razorpays_payment_id(
    rail: RazorpayRail, fake: FakeRazorpay
) -> None:
    _schedule(rail, "pmt_paid")
    pay_id = fake.settle("pmt_paid")

    charge = rail.get_charge(TENANT, PaymentId("pmt_paid"))

    assert charge is not None
    assert (charge.status, charge.rail_payment_id, charge.amount) == ("captured", pay_id, AMOUNT)


def test_a_failed_debit_is_failed_with_razorpays_error_code(
    rail: RazorpayRail, fake: FakeRazorpay
) -> None:
    _schedule(rail, "pmt_declined")
    pay_id = fake.settle("pmt_declined", "failed", "BAD_REQUEST_ERROR")

    charge = rail.get_charge(TENANT, PaymentId("pmt_declined"))

    assert charge is not None
    assert (charge.status, charge.failure_code, charge.rail_payment_id) == (
        "failed",
        "BAD_REQUEST_ERROR",
        pay_id,
    )


def test_a_capture_after_a_failed_attempt_wins(rail: RazorpayRail, fake: FakeRazorpay) -> None:
    _schedule(rail, "pmt_retried")
    fake.settle("pmt_retried", "failed", "BAD_REQUEST_ERROR")
    captured = fake.settle("pmt_retried")

    charge = rail.get_charge(TENANT, PaymentId("pmt_retried"))

    assert charge is not None
    assert (charge.status, charge.rail_payment_id) == ("captured", captured)


def test_an_authorized_payment_is_still_scheduled(rail: RazorpayRail, fake: FakeRazorpay) -> None:
    _schedule(rail, "pmt_authorized")
    fake.settle("pmt_authorized", "authorized")

    charge = rail.get_charge(TENANT, PaymentId("pmt_authorized"))

    assert charge is not None
    assert charge.status == "scheduled"


def test_the_token_expiry_is_read_from_razorpay(rail: RazorpayRail, fake: FakeRazorpay) -> None:
    fake.token_expiry[TOKEN.token_ref] = 1_793_471_399

    assert rail.token_expires_at(TOKEN) == datetime(2026, 10, 31, 18, 29, 59, tzinfo=UTC)


def test_no_key_secret_or_payload_reaches_the_logs(
    rail: RazorpayRail, fake: FakeRazorpay, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _schedule(rail, "pmt_logged")
    _schedule(rail, "pmt_logged_refused", UNKNOWN)
    fake.next_status = 503
    with pytest.raises(RailUnavailable):
        _schedule(rail, "pmt_logged_down")
    fake.settle("pmt_logged", "failed", "BAD_REQUEST_ERROR")
    rail.get_charge(TENANT, PaymentId("pmt_logged"))

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "pmt_logged_refused" in logged  # the refusal is logged, by our reference
    for leak in (KEY_SECRET, KEY_ID, "Authorization", "token_fake001", '"notes"', "{"):
        assert leak not in logged


def test_it_is_built_from_settings() -> None:
    settings = make_settings(
        rail="razorpay",
        razorpay_key_id=KEY_ID,
        razorpay_key_secret=KEY_SECRET,
        razorpay_webhook_secret="whsec",  # noqa: S106
    )

    assert isinstance(RazorpayRail.from_settings(settings), RazorpayRail)


def test_an_order_for_another_amount_is_never_taken_as_the_payments_charge(
    rail: RazorpayRail,
) -> None:
    _schedule(rail, "pmt_amount")

    with pytest.raises(ValueError, match="amount"):
        rail.schedule_charge(
            TENANT,
            TOKEN,
            Money(minor=50_000, currency="INR"),
            PaymentId("pmt_amount"),
            IdempotencyKey("idem_pmt_amount"),
        )


# --- The debit, once the Pre-debit notice period is over (ADR-0017) -------------------


def _debit(rail: RazorpayRail, payment_id: str, token: RailToken = TOKEN):  # type: ignore[no-untyped-def]
    return rail.debit(
        TENANT, token, AMOUNT, PaymentId(payment_id), IdempotencyKey(f"debit_{payment_id}")
    )


def _recurring(fake: FakeRazorpay) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in fake.requests if r.url.path.endswith("/recurring")]


def test_a_debit_before_the_notice_period_ends_is_not_yet(
    rail: RazorpayRail, fake: FakeRazorpay
) -> None:
    _schedule(rail, "pmt_early")
    fake.now = CREATED_AT + NOTICE_SECONDS - 1

    charge = _debit(rail, "pmt_early")

    assert charge is not None
    assert (charge.status, charge.rail_payment_id, charge.failure_code) == ("scheduled", None, None)
    assert charge.debit_after == datetime.fromtimestamp(CREATED_AT + NOTICE_SECONDS, UTC)
    assert fake.payments == {}


def test_a_debit_before_the_notice_is_delivered_is_not_yet(
    rail: RazorpayRail, fake: FakeRazorpay
) -> None:
    _schedule(rail, "pmt_undelivered")
    fake.now = CREATED_AT + NOTICE_SECONDS
    fake.notice_delivered = False

    charge = _debit(rail, "pmt_undelivered")

    assert charge is not None
    assert (charge.status, charge.rail_payment_id) == ("scheduled", None)


def test_a_failed_notice_is_never_debited_and_fails_the_charge(
    rail: RazorpayRail, fake: FakeRazorpay
) -> None:
    fake.failed_notices.add(TOKEN.token_ref)
    _schedule(rail, "pmt_notice_failed")
    fake.now = CREATED_AT + NOTICE_SECONDS

    charge = _debit(rail, "pmt_notice_failed")

    assert charge is not None
    assert (charge.status, charge.failure_code) == ("failed", RAIL_NOTICE_FAILED)
    assert not [r for r in fake.requests if r.url.path.endswith("/payments/create/recurring")]


def test_once_the_notice_period_ends_the_card_is_debited_once(
    rail: RazorpayRail, fake: FakeRazorpay
) -> None:
    _schedule(rail, "pmt_debit")
    fake.now = CREATED_AT + NOTICE_SECONDS

    first = _debit(rail, "pmt_debit")
    again = _debit(rail, "pmt_debit")

    [order] = fake.orders
    [payment] = fake.payments[order["id"]]
    assert first is not None and again is not None
    assert (first.status, first.rail_payment_id) == ("scheduled", payment["id"])
    assert again.rail_payment_id == payment["id"]
    [made] = _recurring(fake)
    assert made["order_id"] == order["id"]
    assert (made["customer_id"], made["token"]) == ("cust_fake001", "token_fake001")
    assert (made["amount"], made["currency"], made["recurring"]) == (49_900, "INR", "1")
    assert made["notes"] == {"agentpay_payment_id": "pmt_debit", "agentpay_tenant_id": "ten_rzp"}
    assert (made["email"], made["contact"]) == (fake.EMAIL, fake.CONTACT)


def test_an_order_already_attempted_is_never_debited_again(
    rail: RazorpayRail, fake: FakeRazorpay
) -> None:
    _schedule(rail, "pmt_attempted")
    fake.now = CREATED_AT + NOTICE_SECONDS
    fake.orders[0]["attempts"] = 1  # a debit Razorpay counted but doesn't list yet

    charge = _debit(rail, "pmt_attempted")

    assert charge is not None and charge.status == "scheduled"
    assert _recurring(fake) == []


def test_a_debit_razorpay_refuses_fails_with_its_code(
    rail: RazorpayRail, fake: FakeRazorpay
) -> None:
    _schedule(rail, "pmt_debit_refused")
    fake.now = CREATED_AT + NOTICE_SECONDS
    fake.refuse_debits = True

    charge = _debit(rail, "pmt_debit_refused")

    assert charge is not None
    assert (charge.status, charge.failure_code) == ("failed", "BAD_REQUEST_ERROR")


def test_there_is_nothing_to_debit_without_an_order(rail: RazorpayRail) -> None:
    assert _debit(rail, "pmt_no_order") is None


def test_the_payers_contact_details_never_reach_the_logs(
    rail: RazorpayRail, fake: FakeRazorpay, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _schedule(rail, "pmt_contact")
    fake.now = CREATED_AT + NOTICE_SECONDS
    fake.refuse_debits = True

    _debit(rail, "pmt_contact")

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "pmt_contact" in logged
    assert fake.EMAIL not in logged and fake.CONTACT not in logged
