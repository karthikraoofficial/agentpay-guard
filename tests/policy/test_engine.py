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
from core.domain.signals import IntentSignal, Signals
from core.policy.engine import decide
from core.policy.packs import PolicyPack, load_pack

# The Phase 1 rules are tested on the pack that introduced them; Signal tests below build
# test packs on top of it. default@1.1.0 itself is covered by tests/policy/test_packs.py
# and the Phase 2 golden set.
PACK = load_pack(Path(__file__).resolve().parents[2] / "policy_packs", "default", "1.0.0").pack
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


# --- Velocity (#38) ----------------------------------------------------------------------

VELOCITY_PACK = PolicyPack.model_validate(
    PACK.model_dump(mode="json", exclude_none=True)
    | {
        "version": "1.0.1",
        "velocity": {"max_decisions": 5, "window_seconds": 600},
        "rules": [*PACK.model_dump(mode="json")["rules"], {"rule_id": "velocity", "enabled": True}],
    }
)


def velocity(count: int | None, **kw: Any):  # type: ignore[no-untyped-def]
    return run(**({"signals": Signals(velocity_count=count), "pack": VELOCITY_PACK} | kw))


def test_four_earlier_decisions_in_the_window_pass_and_five_step_up() -> None:
    assert velocity(4).outcome == "allow"
    assert result_of(velocity(4), "velocity") == "pass"

    output = velocity(5)
    assert output.outcome == "step_up"
    assert output.reason_codes == ("VELOCITY_HIGH",)


def test_a_missing_velocity_count_steps_up_rather_than_passing() -> None:
    output = velocity(None)

    assert output.outcome == "step_up"
    assert output.reason_codes == ("VELOCITY_HIGH",)


def test_velocity_never_turns_a_block_into_a_step_up() -> None:
    output = velocity(9, request=pay(1_250_00, mcc="7995"))

    assert output.outcome == "block"
    assert output.reason_codes == ("MCC_HIGH_RISK", "MCC_NOT_ALLOWED", "VELOCITY_HIGH")


def test_a_phase_1_step_up_stays_the_primary_reason_when_velocity_fires() -> None:
    output = velocity(5, request=pay(6_000_00))

    assert output.reason_codes == ("AMOUNT_ABOVE_STEP_UP_THRESHOLD", "VELOCITY_HIGH")


def test_velocity_is_not_evaluated_when_the_pack_disables_it_or_leaves_it_out() -> None:
    disabled = VELOCITY_PACK.model_copy(
        update={
            "rules": tuple(
                r.model_copy(update={"enabled": False}) if r.rule_id == "velocity" else r
                for r in VELOCITY_PACK.rules
            )
        }
    )
    assert result_of(velocity(9, pack=disabled), "velocity") == "not_evaluated"
    assert run(signals=Signals(velocity_count=9)).outcome == "allow"  # 1.0.0 has no velocity


# --- Intent and Price drift (#42) ---------------------------------------------------------

SIGNALS_PACK = PolicyPack.model_validate(
    VELOCITY_PACK.model_dump(mode="json", exclude_none=True)
    | {
        "version": "1.0.2",
        "intent": {"verdict_ttl_seconds": 1800, "min_tier": "medium"},
        "price_drift": {"max_above_bps": 200},
        "rules": [
            *VELOCITY_PACK.model_dump(mode="json")["rules"],
            {"rule_id": "intent", "enabled": True},
            {"rule_id": "price_drift", "enabled": True},
        ],
    }
)
MEDIUM = 3_000_00  # INR 3,000: medium tier, inside every cap


def verdict(v: str = "match", quoted: int = MEDIUM) -> IntentSignal:
    return IntentSignal(
        intent_id="int_1",
        verdict=v,
        failure_cause=None if v != "unknown" else "timeout",
        quoted_total=inr(quoted),
        model="claude-haiku-5-5",
        prompt_version="intent_check@1",
        content_hash="sha256:" + "ab" * 32,
    )


def with_intent(intent: IntentSignal | None, minor: int = MEDIUM, **kw: Any):  # type: ignore[no-untyped-def]
    resolution = "used" if intent is not None else "none_cited"
    signals = Signals(velocity_count=0, intent=intent, intent_resolution=resolution)
    return run(**({"signals": signals, "pack": SIGNALS_PACK, "request": pay(minor)} | kw))


def test_a_medium_payment_with_a_matching_intent_is_allowed() -> None:
    output = with_intent(verdict("match"))

    assert output.outcome == "allow"
    assert result_of(output, "intent") == "pass"
    assert result_of(output, "price_drift") == "pass"


@pytest.mark.parametrize(
    ("intent", "code"),
    [(None, "INTENT_MISSING"), ("mismatch", "INTENT_MISMATCH"), ("unknown", "INTENT_UNKNOWN")],
)
def test_without_a_usable_match_a_medium_payment_steps_up(intent: str | None, code: str) -> None:
    output = with_intent(None if intent is None else verdict(intent))

    assert (output.outcome, output.reason_codes) == ("step_up", (code,))


def test_a_low_tier_payment_needs_no_intent_check() -> None:
    output = with_intent(None, minor=1_250_00)

    assert output.outcome == "allow"
    assert result_of(output, "intent") == "pass"


def test_a_missing_resolution_is_treated_as_no_usable_intent() -> None:
    output = run(signals=Signals(velocity_count=0), pack=SIGNALS_PACK, request=pay(MEDIUM))

    assert output.reason_codes == ("INTENT_MISSING",)


def test_drift_up_to_two_percent_passes_and_one_minor_unit_more_steps_up() -> None:
    quoted = 3_000_00
    at_limit = quoted * 10_200 // 10_000  # +2.00% exactly

    assert with_intent(verdict(quoted=quoted), minor=at_limit).outcome == "allow"
    over = with_intent(verdict(quoted=quoted), minor=at_limit + 1)
    assert (over.outcome, over.reason_codes) == ("step_up", ("PRICE_DRIFT",))


def test_paying_less_than_the_quote_is_never_drift() -> None:
    assert with_intent(verdict(quoted=4_000_00), minor=MEDIUM).outcome == "allow"


def test_price_drift_is_not_evaluated_without_a_usable_intent() -> None:
    output = with_intent(None, minor=1_250_00)

    assert result_of(output, "price_drift") == "not_evaluated"


def test_a_currency_mismatch_leaves_intent_and_drift_not_evaluated() -> None:
    output = with_intent(verdict(), request=pay(MEDIUM, currency="USD"))

    assert result_of(output, "intent") == "not_evaluated"
    assert result_of(output, "price_drift") == "not_evaluated"


def test_no_signal_turns_a_block_into_anything_else() -> None:
    output = with_intent(verdict("mismatch", quoted=1_00), request=pay(MEDIUM, mcc="7995"))

    assert output.outcome == "block"
    assert output.reason_codes[:2] == ("MCC_HIGH_RISK", "MCC_NOT_ALLOWED")
    assert output.reason_codes[2:] == ("INTENT_MISMATCH", "PRICE_DRIFT")


def test_signal_reasons_follow_velocity_then_intent_then_drift() -> None:
    signals = Signals(
        velocity_count=9, intent=verdict("unknown", quoted=1_00), intent_resolution="used"
    )

    output = run(signals=signals, pack=SIGNALS_PACK, request=pay(6_000_00))

    assert output.reason_codes == (
        "AMOUNT_ABOVE_STEP_UP_THRESHOLD",
        "VELOCITY_HIGH",
        "INTENT_UNKNOWN",
        "PRICE_DRIFT",
    )


# --- New merchant (#39) ------------------------------------------------------------------

MERCHANT_PACK = PolicyPack.model_validate(
    SIGNALS_PACK.model_dump(mode="json", exclude_none=True)
    | {
        "version": "1.0.3",
        "merchant_new": {"min_tier": "medium"},
        "rules": [
            *[
                r
                for r in SIGNALS_PACK.model_dump(mode="json")["rules"]
                if r["rule_id"] not in ("intent", "price_drift")
            ],
            {"rule_id": "merchant_new", "enabled": True},
            {"rule_id": "intent", "enabled": True},
            {"rule_id": "price_drift", "enabled": True},
        ],
    }
)


def merchant(is_new: bool | None, minor: int = MEDIUM, **kw: Any):  # type: ignore[no-untyped-def]
    signals = Signals(
        velocity_count=0, merchant_is_new=is_new, intent=verdict(), intent_resolution="used"
    )
    return run(**({"signals": signals, "pack": MERCHANT_PACK, "request": pay(minor)} | kw))


def test_a_medium_payment_to_a_new_merchant_steps_up_and_a_known_one_passes() -> None:
    assert (merchant(True).outcome, merchant(True).reason_codes) == ("step_up", ("MERCHANT_NEW",))
    assert merchant(False).outcome == "allow"


def test_a_low_tier_first_purchase_is_not_stepped_up() -> None:
    output = merchant(True, minor=1_250_00)

    assert output.outcome == "allow"
    assert result_of(output, "merchant_new") == "pass"


def test_a_missing_merchant_history_is_treated_as_new() -> None:
    assert merchant(None).reason_codes == ("MERCHANT_NEW",)


def test_new_merchant_comes_after_velocity_and_before_intent() -> None:
    signals = Signals(velocity_count=9, merchant_is_new=True, intent_resolution="none_cited")

    output = run(signals=signals, pack=MERCHANT_PACK, request=pay(MEDIUM))

    assert output.reason_codes == ("VELOCITY_HIGH", "MERCHANT_NEW", "INTENT_MISSING")


def test_new_merchant_never_changes_a_block_and_skips_a_currency_mismatch() -> None:
    blocked = merchant(True, request=pay(MEDIUM, mcc="7995"))
    assert blocked.outcome == "block" and "MERCHANT_NEW" in blocked.reason_codes

    mismatch = merchant(True, request=pay(MEDIUM, currency="USD"))
    assert result_of(mismatch, "merchant_new") == "not_evaluated"
