"""Intent checks: a Payer instruction and a proposed cart, compared by a model before the
Agent asks for a Decision (see GLOSSARY.md: Intent check, Intent verdict, Quoted total).

The instruction and cart are kept as one `IntentContent`, hashed as canonical JSON. Only
the hash and the verdict details enter an Audit record, never the text (ADR-0007).
"""

import hashlib
from typing import Annotated, Literal, NewType, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from core.domain.canonical_json import canonical_json
from core.domain.decisions import LineItem, Merchant
from core.domain.mandates import MandateId
from core.domain.money import Money
from core.domain.parties import AgentId, TenantId

IntentId = NewType("IntentId", str)
IntentVerdict = Literal["match", "mismatch", "unknown"]
# Why a verdict is unknown without the model having said so.
FailureCause = Literal["timeout", "error", "unparseable", "disabled"]

PayerInstruction = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IntentContent(_Frozen):
    """What was checked. The Quoted total must equal the line items' sum, in one currency."""

    payer_instruction: PayerInstruction
    merchant: Merchant
    line_items: tuple[LineItem, ...] = Field(min_length=1)
    quoted_total: Money

    @model_validator(mode="after")
    def _total_is_the_sum(self) -> Self:
        currency = self.quoted_total.currency
        if any(item.unit_amount.currency != currency for item in self.line_items):
            raise ValueError("line items and the quoted total must share one currency")
        total = sum(item.quantity * item.unit_amount.minor for item in self.line_items)
        if total != self.quoted_total.minor:
            raise ValueError("quoted_total must equal the sum of quantity x unit_amount")
        return self

    def canonical(self) -> bytes:
        return canonical_json(self)

    def content_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical()).hexdigest()


class IntentCheck(_Frozen):
    intent_id: IntentId
    tenant_id: TenantId
    mandate_id: MandateId
    agent_id: AgentId
    content: IntentContent
    content_hash: str
    verdict: IntentVerdict
    failure_cause: FailureCause | None
    model: str
    prompt_version: str
    # Usage is absent when no reply came back (timeout, error, disabled).
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd_micros: int | None = None
    latency_ms: int | None = None
    created_at: AwareDatetime
    expires_at: AwareDatetime
