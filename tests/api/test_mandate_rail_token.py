"""A Rail token bound to a Mandate when it is granted (#77, ADR-0013)."""

from typing import Any

import pytest

from tests.api.conftest import Api
from tests.api.test_decisions import Pay
from tests.api.test_mandates import Setup
from tests.api.test_tenants_payers_agents import assert_problem

RAIL_TOKEN = {
    "rail": "razorpay",
    "customer_ref": "cust_Mx1aB2cD3eF4gH",
    "token_ref": "token_Mx1aB2cD3eF4gI",
    "display": "visa •••• 1111",
}


@pytest.fixture
def setup(api: Api) -> Setup:
    return Setup(api)


def test_a_mandate_granted_with_a_rail_token_returns_it(setup: Setup) -> None:
    mandate = setup.create(rail_token=RAIL_TOKEN)

    assert mandate["rail_token"] == RAIL_TOKEN
    assert setup.get(mandate["mandate_id"]).json()["rail_token"] == RAIL_TOKEN


def test_a_mandate_without_a_rail_token_reads_as_none(setup: Setup) -> None:
    mandate = setup.create()

    assert mandate["rail_token"] is None
    assert setup.get(mandate["mandate_id"]).json()["rail_token"] is None


def test_a_razorpay_rail_token_needs_an_inr_mandate(setup: Setup) -> None:
    usd = {"minor": 100_00, "currency": "USD"}
    response = setup.api.post(
        "/v1/mandates",
        setup.tenant,
        setup.body(currency="USD", per_txn_cap=usd, monthly_cap=usd, rail_token=RAIL_TOKEN),
    )

    assert_problem(response, 422, "VALIDATION_FAILED")


@pytest.mark.parametrize(
    "field", ["display", "customer_ref", "token_ref"], ids=["display", "customer", "token"]
)
def test_anything_that_looks_like_a_card_number_is_refused(setup: Setup, field: str) -> None:
    token: dict[str, Any] = RAIL_TOKEN | {field: "card_4111111111111111"}
    if field == "display":
        token["display"] = "4111 1111 1111 1111"

    response = setup.api.post("/v1/mandates", setup.tenant, setup.body(rail_token=token))

    assert_problem(response, 422, "VALIDATION_FAILED")
    assert "4111" not in response.text


# --- Evidence: the Mandate snapshot ---------------------------------------------------


def test_a_decision_records_the_rail_token_in_its_mandate_snapshot(api: Api) -> None:
    pay = Pay(api)
    mandate_id = pay.create(rail_token=RAIL_TOKEN)["mandate_id"]

    audit_id = pay.ask(mandate_id=mandate_id).json()["audit_record_id"]

    sealed = pay.audit(audit_id)
    assert sealed.record.inputs.mandate.rail_token.model_dump() == RAIL_TOKEN
    assert b'"token_ref":"token_Mx1aB2cD3eF4gI"' in sealed.body


def test_a_mandate_without_a_rail_token_keeps_the_old_snapshot_bytes(api: Api) -> None:
    pay = Pay(api)

    sealed = pay.audit(pay.ask().json()["audit_record_id"])

    assert b"rail_token" not in sealed.body


def test_a_mock_rail_token_works_in_any_currency(setup: Setup) -> None:
    usd = {"minor": 100_00, "currency": "USD"}
    token = RAIL_TOKEN | {"rail": "mock"}

    mandate = setup.create(currency="USD", per_txn_cap=usd, monthly_cap=usd, rail_token=token)

    assert mandate["rail_token"] == token
