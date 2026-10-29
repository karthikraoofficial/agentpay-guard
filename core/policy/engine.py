"""The deterministic policy engine (non-negotiable 1, ADR-0003).

`decide` is a pure function: no I/O, no clock, no IDs, no model calls. Every enabled rule
in the Policy pack runs in pack order, and the most severe result wins
(block > step_up > allow). Reason codes are ordered by severity, then rule order.
"""

from collections.abc import Callable
from datetime import datetime

from core.domain.decisions import (
    AgentPaymentRequest,
    EngineOutput,
    Outcome,
    ReasonCode,
    RiskTier,
    RuleResult,
)
from core.domain.mandates import Mandate
from core.domain.money import Money
from core.domain.parties import Agent
from core.domain.signals import Signals
from core.policy.pack import PolicyPack


class Facts:
    """The inputs every rule reads."""

    def __init__(
        self,
        request: AgentPaymentRequest,
        mandate: Mandate,
        agent: Agent | None,
        spent_this_month: Money,
        signals: Signals,
        pack: PolicyPack,
        now: datetime,
    ) -> None:
        self.request = request
        self.mandate = mandate
        self.agent = agent
        self.spent_this_month = spent_this_month
        self.signals = signals
        self.pack = pack
        self.now = now


Rule = Callable[[str, Facts], RuleResult]


def _passes(rule_id: str) -> RuleResult:
    return RuleResult(rule_id=rule_id, result="pass")


def _blocks(rule_id: str, code: ReasonCode) -> RuleResult:
    return RuleResult(rule_id=rule_id, result="block", reason_code=code)


def mandate_revoked(rule_id: str, f: Facts) -> RuleResult:
    if f.mandate.revoked_at is not None:
        return _blocks(rule_id, "MANDATE_REVOKED")
    return _passes(rule_id)


def mandate_validity(rule_id: str, f: Facts) -> RuleResult:
    if f.now < f.mandate.valid_from:
        return _blocks(rule_id, "MANDATE_NOT_YET_VALID")
    if f.now >= f.mandate.expires_at:
        return _blocks(rule_id, "MANDATE_EXPIRED")
    return _passes(rule_id)


def agent_allowed(rule_id: str, f: Facts) -> RuleResult:
    if f.request.agent_id not in f.mandate.allowed_agent_ids:
        return _blocks(rule_id, "AGENT_NOT_ALLOWED")
    return _passes(rule_id)


def agent_disabled(rule_id: str, f: Facts) -> RuleResult:
    if f.agent is None:
        return RuleResult(rule_id=rule_id, result="not_evaluated")
    if f.agent.status == "disabled":
        return _blocks(rule_id, "AGENT_DISABLED")
    return _passes(rule_id)


def _steps_up(rule_id: str, code: ReasonCode) -> RuleResult:
    return RuleResult(rule_id=rule_id, result="step_up", reason_code=code)


def _same_currency(f: Facts) -> bool:
    return f.request.amount.currency == f.mandate.currency


def currency_match(rule_id: str, f: Facts) -> RuleResult:
    """Amounts are never compared across currencies: on a mismatch the four amount rules
    are recorded as not evaluated."""
    if not _same_currency(f):
        return _blocks(rule_id, "CURRENCY_MISMATCH")
    return _passes(rule_id)


def mcc_high_risk(rule_id: str, f: Facts) -> RuleResult:
    if f.request.merchant.mcc in f.pack.high_risk_mccs:
        return _blocks(rule_id, "MCC_HIGH_RISK")
    return _passes(rule_id)


def mcc_allowed(rule_id: str, f: Facts) -> RuleResult:
    if f.request.merchant.mcc not in f.mandate.allowed_mccs:
        return _blocks(rule_id, "MCC_NOT_ALLOWED")
    return _passes(rule_id)


def _amount_rule(check: Callable[[str, Facts], RuleResult]) -> Rule:
    def rule(rule_id: str, f: Facts) -> RuleResult:
        if not _same_currency(f):
            return RuleResult(rule_id=rule_id, result="not_evaluated")
        return check(rule_id, f)

    return rule


@_amount_rule
def per_txn_cap(rule_id: str, f: Facts) -> RuleResult:
    """A cap breach blocks; it never steps up, so approval can't widen a Payer's limit."""
    if f.request.amount.minor > f.mandate.per_txn_cap.minor:
        return _blocks(rule_id, "PER_TXN_CAP_EXCEEDED")
    return _passes(rule_id)


@_amount_rule
def monthly_cap(rule_id: str, f: Facts) -> RuleResult:
    if f.spent_this_month.minor + f.request.amount.minor > f.mandate.monthly_cap.minor:
        return _blocks(rule_id, "MONTHLY_CAP_EXCEEDED")
    return _passes(rule_id)


@_amount_rule
def step_up_threshold(rule_id: str, f: Facts) -> RuleResult:
    bands = f.pack.currencies.get(f.request.amount.currency)
    if bands is None or f.request.amount.minor >= bands.step_up_threshold:
        return _steps_up(rule_id, "AMOUNT_ABOVE_STEP_UP_THRESHOLD")
    return _passes(rule_id)


@_amount_rule
def monthly_cap_near(rule_id: str, f: Facts) -> RuleResult:
    after = f.spent_this_month.minor + f.request.amount.minor
    if after * 10_000 >= f.mandate.monthly_cap.minor * f.pack.monthly_cap_near_ratio_bps:
        return _steps_up(rule_id, "MONTHLY_CAP_NEAR")
    return _passes(rule_id)


RULES: dict[str, Rule] = {
    "mandate_revoked": mandate_revoked,
    "mandate_validity": mandate_validity,
    "agent_allowed": agent_allowed,
    "agent_disabled": agent_disabled,
    "currency_match": currency_match,
    "mcc_high_risk": mcc_high_risk,
    "mcc_allowed": mcc_allowed,
    "per_txn_cap": per_txn_cap,
    "monthly_cap": monthly_cap,
    "step_up_threshold": step_up_threshold,
    "monthly_cap_near": monthly_cap_near,
}

_SEVERITY: dict[str, int] = {"block": 2, "step_up": 1, "pass": 0, "not_evaluated": 0}
_OUTCOME: dict[int, Outcome] = {2: "block", 1: "step_up", 0: "allow"}


def risk_tier(amount: Money, pack: PolicyPack) -> RiskTier:
    """Amount bands from the pack. An unpriced currency is high, so it fails closed."""
    bands = pack.currencies.get(amount.currency)
    if bands is None or amount.minor >= bands.step_up_threshold:
        return "high"
    if amount.minor >= bands.medium_from:
        return "medium"
    return "low"


def decide(
    request: AgentPaymentRequest,
    mandate: Mandate,
    agent: Agent | None,
    spent_this_month: Money,
    signals: Signals,
    pack: PolicyPack,
    now: datetime,
) -> EngineOutput:
    facts = Facts(request, mandate, agent, spent_this_month, signals, pack, now)
    results: list[RuleResult] = []
    for setting in pack.rules:
        rule = RULES.get(setting.rule_id)
        if rule is None or not setting.enabled:
            results.append(RuleResult(rule_id=setting.rule_id, result="not_evaluated"))
        else:
            results.append(rule(setting.rule_id, facts))

    fired = [(i, r) for i, r in enumerate(results) if r.reason_code is not None]
    fired.sort(key=lambda pair: (-_SEVERITY[pair[1].result], pair[0]))
    worst = max((_SEVERITY[r.result] for r in results), default=0)
    return EngineOutput(
        outcome=_OUTCOME[worst],
        reason_codes=tuple(r.reason_code for _, r in fired if r.reason_code is not None),
        risk_tier=risk_tier(request.amount, pack),
        rule_results=tuple(results),
    )
