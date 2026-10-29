"""Agent payment requests, Decisions and the engine's output (see GLOSSARY.md)."""

from datetime import UTC, datetime
from typing import Annotated, Literal, NewType, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from core.domain.mandates import MandateId, Mcc
from core.domain.money import Money
from core.domain.parties import AgentId, TenantId

DecisionId = NewType("DecisionId", str)
AuditRecordId = NewType("AuditRecordId", str)

Outcome = Literal["allow", "step_up", "block"]
RiskTier = Literal["low", "medium", "high"]
RuleResultValue = Literal["pass", "step_up", "block", "not_evaluated"]
ReasonCode = Literal[
    "MANDATE_REVOKED",
    "MANDATE_NOT_YET_VALID",
    "MANDATE_EXPIRED",
    "AGENT_NOT_ALLOWED",
    "AGENT_DISABLED",
    "CURRENCY_MISMATCH",
    "MCC_HIGH_RISK",
    "MCC_NOT_ALLOWED",
    "PER_TXN_CAP_EXCEEDED",
    "MONTHLY_CAP_EXCEEDED",
    "AMOUNT_ABOVE_STEP_UP_THRESHOLD",
    "MONTHLY_CAP_NEAR",
    "VELOCITY_HIGH",
]

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Merchant(_Frozen):
    id: _Text
    name: _Text
    mcc: Mcc


class LineItem(_Frozen):
    description: _Text
    quantity: int = Field(ge=1, strict=True)
    unit_amount: Money


class AgentPaymentRequest(_Frozen):
    """What an Agent asks to pay. There is no timestamp (the server's time is used) and
    no Payer (it comes from the Mandate). Quote, line items and Agent reasoning are kept
    as evidence and never read by rules."""

    mandate_id: MandateId
    agent_id: AgentId
    amount: Money
    merchant: Merchant
    quote_id: _Text | None = None
    line_items: tuple[LineItem, ...] | None = None
    agent_reasoning: Annotated[str, StringConstraints(max_length=1000)] | None = None

    @model_validator(mode="after")
    def _positive(self) -> Self:
        if self.amount.minor <= 0:
            raise ValueError("amount must be positive")
        return self


class RuleResult(_Frozen):
    rule_id: str
    result: RuleResultValue
    reason_code: ReasonCode | None = None


class EngineOutput(_Frozen):
    """Everything the policy engine decides. Replay compares these bytes."""

    outcome: Outcome
    reason_codes: tuple[ReasonCode, ...]
    risk_tier: RiskTier
    rule_results: tuple[RuleResult, ...]


class PackRef(_Frozen):
    id: str
    version: str
    hash: str


class Decision(_Frozen):
    decision_id: DecisionId
    tenant_id: TenantId
    mandate_id: MandateId
    agent_id: AgentId
    amount: Money
    outcome: Outcome
    reason_codes: tuple[ReasonCode, ...]
    risk_tier: RiskTier
    policy_pack: PackRef
    audit_record_id: AuditRecordId
    decided_at: AwareDatetime


def month_window(now: datetime) -> tuple[datetime, datetime]:
    """The UTC calendar month containing `now`, as [start, end). Spent this month counts
    allowed Decisions in this window."""
    utc = now.astimezone(UTC)
    start = utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end
