"""The policy engine at its seam: decide(request, mandate, agent, spent, signals, pack, now)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.domain.canonical_json import canonical_json
from core.domain.decisions import AgentPaymentRequest
from core.domain.mandates import Mandate
from core.domain.money import Money
from core.domain.parties import Agent
from core.domain.signals import Signals
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
        "signals": Signals(),
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


# --- Currency, merchant category, caps and step-up (#21) -------------------------

INR = "INR"


def inr(minor: int) -> Money:
    return Money(minor=minor, currency=INR)


def pay(minor: int, mcc: str = "5411", currency: str = INR) -> AgentPaymentRequest:
    return request(
        amount={"minor": minor, "currency": currency},
        merchant={"id": "m_1", "name": "Shop", "mcc": mcc},
    )


def test_a_currency_mismatch_blocks_and_skips_the_amount_rules() -> None:
    output = run(request=pay(100_00, currency="USD"))

    assert output.reason_codes == ("CURRENCY_MISMATCH",)
    for rule_id in ("per_txn_cap", "monthly_cap", "step_up_threshold", "monthly_cap_near"):
        assert result_of(output, rule_id) == "not_evaluated"


def test_a_high_risk_mcc_blocks_even_if_the_mandate_allows_it() -> None:
    output = run(request=pay(100_00, mcc="7995"), mandate=mandate(allowed_mccs=("5411", "7995")))

    assert output.reason_codes == ("MCC_HIGH_RISK",)


def test_an_mcc_outside_the_mandate_blocks() -> None:
    assert run(request=pay(100_00, mcc="5812")).reason_codes == ("MCC_NOT_ALLOWED",)


def test_the_per_transaction_cap_blocks_above_but_not_at_the_cap() -> None:
    capped = mandate(per_txn_cap=inr(4_000_00))

    assert run(request=pay(4_000_00), mandate=capped).outcome == "allow"
    assert run(request=pay(4_000_01), mandate=capped).reason_codes == ("PER_TXN_CAP_EXCEEDED",)


def test_the_monthly_cap_blocks_when_spend_plus_amount_exceeds_it() -> None:
    near_full = {"mandate": mandate(monthly_cap=inr(10_000_00), per_txn_cap=inr(4_000_00))}

    at_cap = run(request=pay(1_000_00), spent_this_month=inr(9_000_00), **near_full)
    over = run(request=pay(1_000_01), spent_this_month=inr(9_000_00), **near_full)

    assert "MONTHLY_CAP_EXCEEDED" not in at_cap.reason_codes
    assert over.reason_codes[0] == "MONTHLY_CAP_EXCEEDED"
    assert over.outcome == "block"


def test_the_step_up_threshold_starts_at_inr_5000() -> None:
    below = run(request=pay(4_999_99))
    at = run(request=pay(5_000_00))

    assert (below.outcome, below.risk_tier) == ("allow", "medium")
    assert (at.outcome, at.reason_codes, at.risk_tier) == (
        "step_up",
        ("AMOUNT_ABOVE_STEP_UP_THRESHOLD",),
        "high",
    )


def test_reaching_80_percent_of_the_monthly_cap_steps_up() -> None:
    cap = {"mandate": mandate(monthly_cap=inr(50_000_00))}

    just_under = run(request=pay(1_000_00), spent_this_month=inr(38_999_99), **cap)
    at = run(request=pay(1_000_00), spent_this_month=inr(39_000_00), **cap)

    assert just_under.outcome == "allow"
    assert (at.outcome, at.reason_codes) == ("step_up", ("MONTHLY_CAP_NEAR",))


def test_risk_tiers_follow_the_pack_bands() -> None:
    assert run(request=pay(1_999_99)).risk_tier == "low"
    assert run(request=pay(2_000_00)).risk_tier == "medium"
    usd = mandate(
        currency="USD",
        per_txn_cap=Money(minor=500_00, currency="USD"),
        monthly_cap=Money(minor=900_00, currency="USD"),
    )
    assert run(request=pay(99_99, currency="USD"), mandate=usd).risk_tier == "medium"
    assert run(request=pay(100_00, currency="USD"), mandate=usd).outcome == "step_up"


def test_blocks_come_before_step_ups_then_rule_order() -> None:
    output = run(
        request=pay(12_000_00, mcc="5812"),
        mandate=mandate(monthly_cap=inr(12_000_00)),
    )

    assert output.outcome == "block"
    assert output.reason_codes == (
        "MCC_NOT_ALLOWED",
        "PER_TXN_CAP_EXCEEDED",
        "AMOUNT_ABOVE_STEP_UP_THRESHOLD",
        "MONTHLY_CAP_NEAR",
    )


def test_a_cap_breach_never_steps_up_instead_of_blocking() -> None:
    output = run(request=pay(10_000_01))

    assert output.outcome == "block"
    assert output.reason_codes[0] == "PER_TXN_CAP_EXCEEDED"


def test_every_rule_is_now_evaluated_for_a_normal_request() -> None:
    output = run()

    assert {r.result for r in output.rule_results} == {"pass"}


def test_signals_are_an_input_and_an_empty_set_changes_nothing() -> None:
    output = decide(request(), mandate(), agent(), ZERO, Signals(), PACK, NOW)

    assert output.outcome == "allow"
    assert canonical_json(output) == canonical_json(run())
