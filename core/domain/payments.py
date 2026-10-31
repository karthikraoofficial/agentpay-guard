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
    # When the Pre-debit notice period ends and the card may be debited (ADR-0017). None
    # until the rail says, and on a rail that debits at once.
    debit_after: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @property
    def rail_answered(self) -> bool:
        """Whether the rail has ever answered for this Payment's charge."""
        return self.rail_payment_id is not None or self.debit_after is not None

    def debit_due(self, now: datetime) -> bool:
        """Whether the card is to be debited now: the notice period is over and no debit
        has been made yet."""
        return (
            self.status == "scheduled"
            and self.rail_payment_id is None
            and self.debit_after is not None
            and self.debit_after <= now
        )

    def charged(
        self,
        status: PaymentStatus,
        rail_payment_id: str | None,
        failure_code: str | None,
        at: datetime,
        debit_after: datetime | None = None,
    ) -> Self | None:
        """This Payment after the rail reported on its charge, or None when that changes
        nothing. A settled Payment never reopens, so a late or repeated report is a no-op.
        While scheduled, only news is applied: a first rail payment id or debit time."""
        if self.status != "scheduled":
            return None
        news = (rail_payment_id is not None and self.rail_payment_id is None) or (
            debit_after is not None and self.debit_after is None
        )
        if status == "scheduled" and not news:
            return None
        return self.model_copy(
            update={
                "status": status,
                "rail_payment_id": self.rail_payment_id or rail_payment_id,
                "failure_code": failure_code if status == "failed" else None,
                "debit_after": self.debit_after or debit_after,
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
    if stop := debit_refusal(mandate_status=mandate_status, agent_disabled=agent_disabled):
        return stop
    if rail_token is None or rail_token.rail != rail:
        return "MANDATE_NOT_PAYABLE"
    return None


def debit_refusal(*, mandate_status: MandateStatus, agent_disabled: bool) -> PaymentRefusal | None:
    """Why the card can't be debited for a Payment now, or None if it can: the Mandate
    must still be active and the Agent enabled, at payment time and again at the debit."""
    if mandate_status != "active":
        return _MANDATE_REFUSALS[mandate_status]
    if agent_disabled:
        return "AGENT_DISABLED"
    return None


SettledStatus = Literal["captured", "failed"]


class RailReport(BaseModel):
    """What a rail event says about one of our Payments, read from the rail's own format by
    its adapter. Only the fields we act on or keep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str
    payment_id: PaymentId
    rail_payment_id: str
    status: SettledStatus
    amount: Money
    failure_code: str | None = None

    def to_event(self, event_id: str, tenant_id: TenantId, at: datetime) -> "RailEvent":
        return RailEvent(
            event_id=event_id,
            tenant_id=tenant_id,
            event_type=self.event_type,
            payment_id=self.payment_id,
            rail_payment_id=self.rail_payment_id,
            status=self.status,
            failure_code=self.failure_code,
            received_at=at,
        )


class RailEvent(BaseModel):
    """A rail event we acted on, kept so a redelivery is recognised. Never the payload:
    no card, contact or personal details (non-negotiable 7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    tenant_id: TenantId
    event_type: str
    payment_id: PaymentId
    rail_payment_id: str
    status: SettledStatus
    failure_code: str | None
    received_at: AwareDatetime
