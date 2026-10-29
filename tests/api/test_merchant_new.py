"""The New merchant Signal over HTTP, on a test pack that adds only it to 1.0.0 (#39)."""

from pathlib import Path
from typing import Any

import pytest

from tests.api.conftest import Api
from tests.api.test_decisions import Pay
from tests.packs import merchant_new_pack

LOW, MEDIUM = 1_250_00, 3_000_00
SHOP = {"id": "m_fresh", "name": "FreshMart", "mcc": "5411"}


@pytest.fixture
def pay(api: Api, tmp_path: Path) -> Pay:
    api.use_packs(merchant_new_pack(tmp_path))
    return Pay(api)


def spend(pay: Pay, minor: int, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = pay.ask(
        amount={"minor": minor, "currency": "INR"}, merchant=SHOP, **overrides
    ).json()
    return body


def outcome(body: dict[str, Any]) -> tuple[str, list[str]]:
    return body["outcome"], body["reason_codes"]


def test_a_first_medium_payment_to_a_merchant_steps_up(pay: Pay) -> None:
    assert outcome(spend(pay, MEDIUM)) == ("step_up", ["MERCHANT_NEW"])


def test_once_a_payment_is_allowed_the_merchant_is_known(pay: Pay) -> None:
    assert outcome(spend(pay, LOW)) == ("allow", [])

    assert outcome(spend(pay, MEDIUM)) == ("allow", [])


def test_history_carries_over_to_a_superseding_mandate(pay: Pay) -> None:
    spend(pay, LOW)
    newer = pay.create(supersedes=pay.mandate_id)["mandate_id"]

    assert outcome(spend(pay, MEDIUM, mandate_id=newer)) == ("allow", [])


def test_blocked_and_stepped_up_payments_do_not_make_a_merchant_known(pay: Pay) -> None:
    blocked = spend(pay, LOW, agent_id="agt_unknown")
    assert blocked["outcome"] == "block"
    assert spend(pay, MEDIUM)["outcome"] == "step_up"

    assert outcome(spend(pay, MEDIUM)) == ("step_up", ["MERCHANT_NEW"])


def test_another_payers_history_in_the_same_tenant_does_not_count(pay: Pay) -> None:
    spend(pay, LOW)
    other_payer = pay.api.post(
        "/v1/payers", pay.tenant, {"display_name": "Ravi", "contact_ref": "tok_2"}
    ).json()["payer_id"]
    theirs = pay.create(payer_id=other_payer)["mandate_id"]

    assert outcome(spend(pay, MEDIUM, mandate_id=theirs)) == ("step_up", ["MERCHANT_NEW"])


def test_the_audit_record_and_signals_span_carry_whether_the_merchant_was_new(pay: Pay) -> None:
    first = spend(pay, LOW)
    second = spend(pay, MEDIUM)

    flags = [
        pay.audit(b["audit_record_id"]).record.inputs.signals.merchant_is_new
        for b in (first, second)
    ]
    assert flags == [True, False]
    spans = [s for s in pay.api.telemetry.spans if s.name == "signals.compute"]
    assert [s.attributes["merchant_is_new"] for s in spans] == [True, False]
