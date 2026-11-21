"""Contract tests for `create_payment` and `get_payment` (#248): the published input
schemas, a successful call, API errors as tool errors, and idempotent replay; and, end to
end on the mock rail, an allow paid through `create_payment` and read back captured."""

from pathlib import Path
from typing import Any

import pytest

from adapters.memory.rail import MockRail
from core.debits import run_debit_step
from core.domain.payments import PaymentCreate, PaymentId
from core.reconcile import sweep
from tests.api.conftest import Api
from tests.api.test_debits import NOTICE, SWEEP_AGE
from tests.api.test_payments import Payments
from tests.mcp.conftest import connect, key_of, payload, text
from tests.packs import phase_1_packs

pytestmark = pytest.mark.anyio


class Buyer(Payments):
    """A Tenant whose Mandate's mock Rail token captures, and a decide-only key for its
    Agent's MCP server."""

    def __init__(self, api: Api, name: str = "acme") -> None:
        super().__init__(api, name)
        self.key = key_of(api.new_key(self.tenant_id(), {"decide"}))

    def decision(self, idempotency_key: str = "buy-1", **overrides: Any) -> dict[str, Any]:
        return {
            "mandate_id": self.mandate_id,
            "agent_id": self.agent_ids[0],
            "amount": {"minor": 1_250_00, "currency": "INR"},
            "merchant": {"id": "m_1", "name": "FreshMart", "mcc": "5411"},
            "idempotency_key": idempotency_key,
        } | overrides


@pytest.fixture
def buyer(api: Api, tmp_path: Path) -> Buyer:
    api.use_packs(phase_1_packs(tmp_path))
    return Buyer(api)


def _pay(decision_id: str, idempotency_key: str = "pay-1") -> dict[str, Any]:
    return {"decision_id": decision_id, "idempotency_key": idempotency_key}


# --- schemas ---------------------------------------------------------------------------


async def test_create_payment_takes_the_apis_request_model_and_an_idempotency_key(
    buyer: Buyer,
) -> None:
    async with connect(buyer.api, buyer.key) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    schema = dict(tools["create_payment"].input_schema)
    properties = dict(schema.pop("properties"))
    assert properties.pop("idempotency_key")["type"] == "string"
    required = schema.pop("required")
    expected = PaymentCreate.model_json_schema()
    assert properties == expected.pop("properties")
    assert set(required) == set(expected.pop("required")) | {"idempotency_key"}
    assert schema == expected
    assert "idempotency key" in (tools["create_payment"].description or "").lower()


async def test_get_payment_takes_only_a_payment_id(buyer: Buyer) -> None:
    async with connect(buyer.api, buyer.key) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    schema = tools["get_payment"].input_schema
    assert schema["required"] == ["payment_id"]
    assert set(schema["properties"]) == {"payment_id"}


# --- create_payment --------------------------------------------------------------------


async def test_paying_an_allow_returns_the_apis_payment(buyer: Buyer) -> None:
    decision = buyer.allow()
    async with connect(buyer.api, buyer.key) as client:
        result = await client.call_tool("create_payment", _pay(decision["decision_id"]))

    assert not result.is_error
    body = payload(result)
    assert body["decision_id"] == decision["decision_id"]
    assert body["amount"] == {"minor": 1_250_00, "currency": "INR"}  # the Decision's
    assert body["status"] in {"scheduled", "captured"}
    assert body == buyer.payment(body["payment_id"]).json()
    assert buyer.charges(body["payment_id"]) == 1


async def test_a_retry_with_the_same_key_returns_the_same_payment_and_charges_once(
    buyer: Buyer,
) -> None:
    decision_id = buyer.allow()["decision_id"]
    async with connect(buyer.api, buyer.key) as client:
        first = payload(await client.call_tool("create_payment", _pay(decision_id)))
        again = payload(await client.call_tool("create_payment", _pay(decision_id)))

    assert again == first
    assert buyer.charges(first["payment_id"]) == 1


async def test_a_second_payment_on_one_decision_is_the_apis_refusal(buyer: Buyer) -> None:
    decision_id = buyer.allow()["decision_id"]
    async with connect(buyer.api, buyer.key) as client:
        first = payload(await client.call_tool("create_payment", _pay(decision_id, "pay-1")))
        second = await client.call_tool("create_payment", _pay(decision_id, "pay-2"))

    assert second.is_error
    assert text(second).startswith("DECISION_ALREADY_PAID: ")
    assert payload(second)["code"] == "DECISION_ALREADY_PAID"
    assert buyer.charges(first["payment_id"]) == 1


async def test_a_pending_step_up_is_not_payable(buyer: Buyer) -> None:
    async with connect(buyer.api, buyer.key) as client:
        stepped = payload(
            await client.call_tool(
                "request_decision",
                buyer.decision(amount={"minor": 6_000_00, "currency": "INR"}),
            )
        )
        assert stepped["outcome"] == "step_up", stepped
        result = await client.call_tool("create_payment", _pay(stepped["decision_id"]))

    assert result.is_error
    assert text(result).startswith("DECISION_NOT_PAYABLE: ")


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (_pay("dec_unknown"), "DECISION_NOT_FOUND"),
        ({"decision_id": "dec_unknown"}, "IDEMPOTENCY_KEY_MISSING"),
        (_pay("dec_unknown") | {"amount": {"minor": 1, "currency": "INR"}}, "VALIDATION_FAILED"),
    ],
    ids=["unknown-decision", "no-idempotency-key", "extra-field"],
)
async def test_an_api_error_is_a_tool_error_with_the_apis_code(
    buyer: Buyer, arguments: dict[str, Any], code: str
) -> None:
    async with connect(buyer.api, buyer.key) as client:
        result = await client.call_tool("create_payment", arguments)

    assert result.is_error
    assert text(result).startswith(f"{code}: ")


# --- get_payment -----------------------------------------------------------------------


async def test_get_payment_reads_as_the_api_reports_it(buyer: Buyer) -> None:
    paid = buyer.pay(buyer.allow()["decision_id"]).json()
    async with connect(buyer.api, buyer.key) as client:
        result = await client.call_tool("get_payment", {"payment_id": paid["payment_id"]})

    assert not result.is_error
    assert payload(result) == buyer.payment(paid["payment_id"]).json()


@pytest.mark.parametrize("payment_id", ["pmt_unknown", "../decisions"])
async def test_an_unknown_payment_is_a_tool_error(buyer: Buyer, payment_id: str) -> None:
    async with connect(buyer.api, buyer.key) as client:
        result = await client.call_tool("get_payment", {"payment_id": payment_id})

    assert result.is_error
    if payment_id == "pmt_unknown":
        assert text(result).startswith("PAYMENT_NOT_FOUND: ")


async def test_another_tenants_payment_is_not_found(buyer: Buyer) -> None:
    theirs = Buyer(buyer.api, "other")
    their_payment = theirs.pay(theirs.allow()["decision_id"]).json()
    async with connect(buyer.api, buyer.key) as client:
        result = await client.call_tool("get_payment", {"payment_id": their_payment["payment_id"]})

    assert result.is_error
    assert payload(result)["code"] == "PAYMENT_NOT_FOUND"


# --- end to end on the mock rail -------------------------------------------------------


@pytest.fixture
def noticed_buyer(api: Api, tmp_path: Path) -> Buyer:
    """The mock rail with the card Pre-debit notice period (ADR-0017), on a clock the test
    moves, as in tests/api/test_end_to_end.py."""
    api.rail = MockRail(notice=NOTICE, clock=api.clock)
    api.use_packs(phase_1_packs(tmp_path))
    return Buyer(api)


async def test_an_allow_paid_through_mcp_is_read_back_captured(noticed_buyer: Buyer) -> None:
    buyer, api = noticed_buyer, noticed_buyer.api
    async with connect(api, buyer.key) as client:
        decision = payload(await client.call_tool("request_decision", buyer.decision()))
        assert decision["outcome"] == "allow", decision
        created = payload(await client.call_tool("create_payment", _pay(decision["decision_id"])))
        payment_id = created["payment_id"]
        assert created["status"] == "scheduled"

        # The notice period ends, the worker debits the card, and the rail captures.
        api.clock.advance(NOTICE)
        run_debit_step(api.store, api.rail, api.clock)
        api.rail.settle(buyer.tenant_id(), PaymentId(payment_id))
        api.clock.advance(SWEEP_AGE)
        sweep(api.store, api.rail, api.clock, SWEEP_AGE)

        read = await client.call_tool("get_payment", {"payment_id": payment_id})

    assert not read.is_error
    captured = payload(read)
    assert (captured["status"], captured["decision_id"]) == ("captured", decision["decision_id"])
    assert captured["rail_payment_id"] is not None
    assert buyer.charges(payment_id) == 1
