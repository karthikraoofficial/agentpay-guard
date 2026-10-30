"""Payments: money moving for an allowed Decision or an approved step-up (see GLOSSARY.md).

A Payment is always for exactly its Decision's amount, there is at most one per Decision
(ADR-0014), and its status only moves forward: scheduled, then captured or failed."""

from datetime import datetime
from typing import Literal, NewType, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict

from core.domain.decisions import DecisionId
from core.domain.mandates import MandateId, MandateStatus
from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.rails import RailName, RailToken

PaymentId = NewType("PaymentId", str)
PaymentStatus = Literal["scheduled", "captured", "failed"]

# Why a Payment can't be made. The Mandate and Agent codes are the Reason codes a Decision
# would give for the same cause.
PaymentRefusal = Literal[
    "DECISION_NOT_PAYABLE",
    "DECISION_ALREADY_PAID",
    "PAYMENT_WINDOW_CLOSED",
    "MANDATE_REVOKED",
    "MANDATE_EXPIRED",
    "MANDATE_NOT_YET_VALID",
    "AGENT_DISABLED",
    "MANDATE_NOT_PAYABLE",
]

_MANDATE_REFUSALS: dict[MandateStatus, PaymentRefusal] = {
    "revoked": "MANDATE_REVOKED",
    "expired": "MANDATE_EXPIRED",
    "not_yet_valid": "MANDATE_NOT_YET_VALID",
}


class Payment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    payment_id: PaymentId
    tenant_id: TenantId
    decision_id: DecisionId
    mandate_id: MandateId
    amount: Money
    rail: RailName
    status: PaymentStatus
    # The rail's own id for the charge, once the rail has accepted it.
    rail_payment_id: str | None = None
    # The rail's code, only on a failed Payment.
    failure_code: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    def charged(
        self,
        status: PaymentStatus,
        rail_payment_id: str | None,
        failure_code: str | None,
        at: datetime,
    ) -> Self | None:
        """This Payment after the rail reported on its charge, or None when that changes
        nothing. A settled Payment never reopens, so a late or repeated report is a no-op."""
        if self.status != "scheduled" or (
            status == "scheduled" and (rail_payment_id is None or self.rail_payment_id)
        ):
            return None
        return self.model_copy(
            update={
                "status": status,
                "rail_payment_id": rail_payment_id or self.rail_payment_id,
                "failure_code": failure_code if status == "failed" else None,
                "updated_at": at,
            }
        )


def payment_refusal(
    *,
    payable_until: datetime | None,
    already_paid: bool,
    mandate_status: MandateStatus,
    agent_disabled: bool,
    rail_token: RailToken | None,
    rail: RailName,
    now: datetime,
) -> PaymentRefusal | None:
    """Why the Decision can't be paid at `now`, or None if it can. `payable_until` is None
    unless the Decision was allowed or its step-up approved."""
    if payable_until is None:
        return "DECISION_NOT_PAYABLE"
    if already_paid:
        return "DECISION_ALREADY_PAID"
    if now >= payable_until:
        return "PAYMENT_WINDOW_CLOSED"
    if mandate_status != "active":
        return _MANDATE_REFUSALS[mandate_status]
    if agent_disabled:
        return "AGENT_DISABLED"
    if rail_token is None or rail_token.rail != rail:
        return "MANDATE_NOT_PAYABLE"
    return None
