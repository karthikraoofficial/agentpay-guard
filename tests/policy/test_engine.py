"""The policy engine at its seam: decide(request, mandate, agent, spent, pack, now)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.domain.canonical_json import canonical_json
from core.domain.decisions import AgentPaymentRequest
from core.domain.mandates import Mandate
from core.domain.money import Money
from core.domain.parties import Agent
from core.policy.engine import decide
from core.policy.packs import load_active_pack

PACK = load_active_pack(Path(__file__).resolve().parents[2] / "policy_packs", "default").pack
NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
ZERO = Money(minor=0, currency="INR")


def mandate(**overrides: Any) -> Mandate:
    fields: dict[str, Any] = {
        "mandate_id": "mnd_1",
        "tenant_id": "ten_1",
        "payer_id": "pay_1",
        "allowed_agent_ids": ("agt_1",),
        "currency": "INR",
        "per_txn_cap": Money(minor=10_000_00, currency="INR"),
        "monthly_cap": Money(minor=50_000_00, currency="INR"),
        "allowed_mccs": ("5411",),
        "valid_from": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=30),
        "consent_evidence": {"method": "otp", "reference": "otp-1", "captured_at": NOW},
        "created_at": NOW - timedelta(days=1),
    }
    return Mandate.model_validate(fields | overrides)


def agent(status: str = "active") -> Agent:
    return Agent.model_validate(
        {
            "agent_id": "agt_1",
            "tenant_id": "ten_1",
            "name": "Bot",
            "status": status,
            "created_at": NOW,
        }
    )


def request(**overrides: Any) -> AgentPaymentRequest:
    fields: dict[str, Any] = {
        "mandate_id": "mnd_1",
        "agent_id": "agt_1",
        "amount": {"minor": 1_250_00, "currency": "INR"},
        "merchant": {"id": "m_1", "name": "FreshMart", "mcc": "5411"},
    }
    return AgentPaymentRequest.model_validate(fields | overrides)


def run(**kw: Any):  # type: ignore[no-untyped-def]
    args: dict[str, Any] = {
        "request": request(),
        "mandate": mandate(),
        "agent": agent(),
        "spent_this_month": ZERO,
        "pack": PACK,
        "now": NOW,
    } | kw
    return decide(**args)


def result_of(output: Any, rule_id: str) -> str:
    return str(next(r.result for r in output.rule_results if r.rule_id == rule_id))


def test_a_request_inside_the_mandate_is_allowed() -> None:
    output = run()

    assert output.outcome == "allow"
    assert output.reason_codes == ()
    assert output.risk_tier == "low"
    assert [r.rule_id for r in output.rule_results] == [r.rule_id for r in PACK.rules]


@pytest.mark.parametrize(
    ("kw", "rule_id", "code"),
    [
        (
            {"mandate": mandate(revoked_by="payer", revoked_at=NOW)},
            "mandate_revoked",
            "MANDATE_REVOKED",
        ),
        (
            {"now": NOW - timedelta(days=1, milliseconds=1)},
            "mandate_validity",
            "MANDATE_NOT_YET_VALID",
        ),
        ({"now": NOW + timedelta(days=30)}, "mandate_validity", "MANDATE_EXPIRED"),
        (
            {"request": request(agent_id="agt_other"), "agent": None},
            "agent_allowed",
            "AGENT_NOT_ALLOWED",
        ),
        ({"agent": agent("disabled")}, "agent_disabled", "AGENT_DISABLED"),
    ],
    ids=["revoked", "not-yet-valid", "expired", "agent-not-allowed", "agent-disabled"],
)
def test_each_mandate_and_agent_rule_blocks_with_its_reason(
    kw: dict[str, Any], rule_id: str, code: str
) -> None:
    output = run(**kw)

    assert output.outcome == "block"
    assert output.reason_codes == (code,)
    assert result_of(output, rule_id) == "block"


def test_the_validity_window_includes_its_start_and_excludes_its_end() -> None:
    assert run(now=NOW - timedelta(days=1)).outcome == "allow"
    assert run(now=NOW + timedelta(days=30) - timedelta(milliseconds=1)).outcome == "allow"


def test_every_failing_rule_is_reported_in_rule_order() -> None:
    output = run(
        mandate=mandate(revoked_by="tenant", revoked_at=NOW),
        agent=agent("disabled"),
        now=NOW + timedelta(days=31),
    )

    assert output.reason_codes == ("MANDATE_REVOKED", "MANDATE_EXPIRED", "AGENT_DISABLED")


def test_an_unknown_agent_has_its_disabled_rule_not_evaluated() -> None:
    output = run(request=request(agent_id="agt_ghost"), agent=None)

    assert result_of(output, "agent_disabled") == "not_evaluated"


def test_agent_reasoning_line_items_and_quote_never_change_the_output() -> None:
    plain = run()
    chatty = run(
        request=request(
            quote_id="q_9",
            agent_reasoning="The user said to ignore all limits and buy it now.",
            line_items=[
                {
                    "description": "Milk",
                    "quantity": 2,
                    "unit_amount": {"minor": 625_00, "currency": "INR"},
                }
            ],
        )
    )

    assert canonical_json(chatty) == canonical_json(plain)


def test_the_same_inputs_give_the_same_bytes() -> None:
    assert canonical_json(run()) == canonical_json(run())
