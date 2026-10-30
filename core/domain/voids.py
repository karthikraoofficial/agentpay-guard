"""Voids: an allow, or an approved step-up, released from Spent this month (see
GLOSSARY.md). A Void is its own Audit record naming the Decision; the Decision never
changes, and a Voided Decision can no longer be paid."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from core.domain.decisions import AuditRecordId, DecisionId
from core.domain.parties import TenantId
from core.domain.payments import PaymentId

# Who voided it: the Tenant through the API, or AgentPay Guard when its Payment failed.
VoidVia = Literal["tenant_api", "payment_failed"]

VoidRefusal = Literal["DECISION_ALREADY_VOIDED", "DECISION_HAS_PAYMENT", "DECISION_NOT_PAYABLE"]


class Void(BaseModel):
    """A Void as stored for lookup beside its Audit record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    decision_id: DecisionId
    via: VoidVia
    # The failed Payment, when the Void was written because of it.
    payment_id: PaymentId | None = None
    voided_at: AwareDatetime
    audit_record_id: AuditRecordId


def void_refusal(
    *, already_voided: bool, has_payment: bool, was_payable: bool
) -> VoidRefusal | None:
    """Why a Tenant can't void a Decision, or None if it can. `was_payable` is whether the
    Decision was allowed or its step-up approved, whether or not its window has closed."""
    if already_voided:
        return "DECISION_ALREADY_VOIDED"
    if has_payment:
        return "DECISION_HAS_PAYMENT"
    if not was_payable:
        return "DECISION_NOT_PAYABLE"
    return None
