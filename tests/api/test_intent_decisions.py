"""Decisions that cite an Intent check, over HTTP on a test pack (#42).

Every way an Intent verdict can be missing or unusable must end in step_up at medium
tier, never allow (the Phase 2 exit criterion)."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core.ports import ModelTimeout, ModelUnavailable
from tests.api.conftest import Api
from tests.api.test_intents import Intents
from tests.conftest import make_settings
from tests.packs import signals_pack

MEDIUM = 3_000_00  # INR 3,000
INSTRUCTION = "Weekly groceries from FreshMart, about 3,000 rupees"


def groceries(minor: int = MEDIUM) -> dict[str, Any]:
    money = {"minor": minor, "currency": "INR"}
    return {
        "payer_instruction": INSTRUCTION,
        "merchant": {"id": "m_1", "name": "FreshMart", "mcc": "5411"},
        "line_items": [{"description": "Groceries", "quantity": 1, "unit_amount": money}],
        "quoted_total": money,
    }


class Flow(Intents):
    def intent(self, **overrides: Any) -> str:
        response = self.check(**(groceries() | overrides))
        assert response.status_code == 201, response.text
        intent_id: str = response.json()["intent_id"]
        return intent_id

    def pay(self, minor: int = MEDIUM, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = self.ask(
            amount={"minor": minor, "currency": "INR"}, **overrides
        ).json()
        return body


@pytest.fixture
def flow(api: Api, tmp_path: Path) -> Flow:
    api.use_packs(signals_pack(tmp_path))
    return Flow(api)


def outcome(body: dict[str, Any]) -> tuple[str, list[str]]:
    return body["outcome"], body["reason_codes"]


def test_a_medium_payment_citing_a_matching_intent_is_allowed(flow: Flow) -> None:
    assert outcome(flow.pay(intent_id=flow.intent())) == ("allow", [])


def test_a_low_tier_payment_needs_no_intent(flow: Flow) -> None:
    assert outcome(flow.pay(minor=1_250_00)) == ("allow", [])


# --- The fail-closed matrix -------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [ModelTimeout("slow"), ModelUnavailable("down"), "Sure, it matches!"],
    ids=["timeout", "gateway-error", "unparseable"],
)
def test_a_failed_check_steps_up_as_unknown(flow: Flow, failure: Any) -> None:
    flow.api.gateway.script.append(failure)

    assert outcome(flow.pay(intent_id=flow.intent())) == ("step_up", ["INTENT_UNKNOWN"])


def test_a_check_made_while_disabled_steps_up_as_unknown(flow: Flow) -> None:
    flow.api.use_settings(make_settings(intent_check_enabled=False))

    assert outcome(flow.pay(intent_id=flow.intent())) == ("step_up", ["INTENT_UNKNOWN"])


def test_a_mismatch_steps_up(flow: Flow) -> None:
    flow.api.gateway.script.append("MISMATCH")

    assert outcome(flow.pay(intent_id=flow.intent())) == ("step_up", ["INTENT_MISMATCH"])


def test_no_intent_steps_up_as_missing(flow: Flow) -> None:
    assert outcome(flow.pay()) == ("step_up", ["INTENT_MISSING"])


def test_an_unknown_intent_id_steps_up_as_missing(flow: Flow) -> None:
    assert outcome(flow.pay(intent_id="int_nope")) == ("step_up", ["INTENT_MISSING"])


def test_an_expired_intent_steps_up_as_missing(flow: Flow) -> None:
    intent_id = flow.intent()
    flow.api.clock.advance(timedelta(minutes=30))

    assert outcome(flow.pay(intent_id=intent_id)) == ("step_up", ["INTENT_MISSING"])


def test_an_intent_for_another_mandate_steps_up_as_missing(flow: Flow) -> None:
    other = flow.create()["mandate_id"]
    intent_id = flow.intent(mandate_id=other)

    assert outcome(flow.pay(intent_id=intent_id)) == ("step_up", ["INTENT_MISSING"])


def test_an_intent_for_another_agent_steps_up_as_missing(flow: Flow) -> None:
    intent_id = flow.intent(agent_id=flow.agent_ids[1])

    assert outcome(flow.pay(intent_id=intent_id)) == ("step_up", ["INTENT_MISSING"])


@pytest.mark.parametrize(
    "merchant",
    [
        {"id": "m_other", "name": "FreshMart", "mcc": "5411"},
        {"id": "m_1", "name": "FreshMart", "mcc": "5812"},
    ],
    ids=["merchant-id", "declared-mcc"],
)
def test_an_intent_for_another_merchant_steps_up_as_missing(
    flow: Flow, merchant: dict[str, str]
) -> None:
    intent_id = flow.intent(merchant=merchant)

    assert outcome(flow.pay(intent_id=intent_id)) == ("step_up", ["INTENT_MISSING"])


def test_another_tenants_intent_steps_up_as_missing(flow: Flow) -> None:
    other = Flow(flow.api, "other")
    intent_id = other.intent()

    assert outcome(flow.pay(intent_id=intent_id)) == ("step_up", ["INTENT_MISSING"])


# --- Price drift -----------------------------------------------------------------------


def test_paying_more_than_two_percent_over_the_quote_steps_up(flow: Flow) -> None:
    intent_id = flow.intent()

    assert outcome(flow.pay(minor=MEDIUM * 102 // 100, intent_id=intent_id)) == ("allow", [])
    over = flow.pay(minor=MEDIUM * 102 // 100 + 1, intent_id=intent_id)
    assert outcome(over) == ("step_up", ["PRICE_DRIFT"])


# --- Evidence --------------------------------------------------------------------------


def test_the_audit_record_has_the_verdict_and_hash_but_never_the_instruction(flow: Flow) -> None:
    intent_id = flow.intent()
    body = flow.pay(intent_id=intent_id)

    sealed = flow.audit(body["audit_record_id"])
    signals = sealed.record.inputs.signals
    assert (signals.cited_intent_id, signals.intent_resolution) == (intent_id, "used")
    assert signals.intent is not None and signals.intent.verdict == "match"
    stored = flow.read(intent_id).json()
    assert signals.intent.content_hash == stored["content_hash"]
    assert INSTRUCTION.encode() not in sealed.body
    assert b"FreshMart" in sealed.body  # the request's own merchant is still evidence


def test_an_unusable_intent_records_why(flow: Flow) -> None:
    intent_id = flow.intent()
    flow.api.clock.advance(timedelta(hours=1))

    signals = flow.audit(flow.pay(intent_id=intent_id)["audit_record_id"]).record.inputs.signals

    assert (signals.cited_intent_id, signals.intent_resolution, signals.intent) == (
        intent_id,
        "expired",
        None,
    )


def test_the_signals_span_carries_the_resolution_and_verdict(flow: Flow) -> None:
    flow.pay(intent_id=flow.intent())

    (span,) = [s for s in flow.api.telemetry.spans if s.name == "signals.compute"]
    assert span.attributes["intent_resolution"] == "used"
    assert span.attributes["intent_verdict"] == "match"
