"""Refunds: money returned on a captured Payment, in full or in parts (see GLOSSARY.md,
PRD #205). Not a Void, which releases a Decision that was never paid.

A Refund is started only by the Tenant, in our API (ADR-0024). It is sent to the rail keyed
by its own id, so asking again never refunds twice, and its status only moves forward:
pending, then processed or failed."""

import hashlib
from datetime import datetime, timedelta
from typing import Literal, NewType, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict

from core.domain.decisions import DecisionId, PackRef
from core.domain.disputes import DisputeStatus
from core.domain.mandates import MandateId
from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import Payment, PaymentId

RefundId = NewType("RefundId", str)
RefundStatus = Literal["pending", "processed", "failed"]
REASON_MAX = 500


class Refund(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    refund_id: RefundId
    tenant_id: TenantId
    payment_id: PaymentId
    decision_id: DecisionId
    mandate_id: MandateId
    amount: Money
    # The Tenant's own words, if any. Never put in the Audit chain: only its hash is
    # (ADR-0007), so it can be erased.
    reason: str | None = None
    status: RefundStatus
    # The rail's own id for the refund, once the rail has accepted it.
    rail_refund_id: str | None = None
    # The rail's code, only on a failed Refund.
    failure_code: str | None = None
    # The Policy pack whose Refund window allowed it.
    pack: PackRef
    created_at: AwareDatetime
    updated_at: AwareDatetime

    def settled(
        self,
        status: RefundStatus,
        rail_refund_id: str | None,
        failure_code: str | None,
        at: datetime,
    ) -> Self | None:
        """This Refund after the rail answered, or None when that changes nothing. A settled
        Refund never reopens, so a late or repeated answer is a no-op. While pending, only
        news is applied: a first rail refund id, which says the rail has it."""
        if self.status != "pending":
            return None
        news = rail_refund_id is not None and self.rail_refund_id is None
        if status == "pending" and not news:
            return None
        return self.model_copy(
            update={
                "status": status,
                "rail_refund_id": self.rail_refund_id or rail_refund_id,
                "failure_code": failure_code if status == "failed" else None,
                "updated_at": at,
            }
        )


RefundRefusal = Literal[
    "PAYMENT_NOT_REFUNDABLE", "PAYMENT_DISPUTED", "REFUND_WINDOW_CLOSED", "REFUND_EXCEEDS_CAPTURED"
]
# A Dispute in these states may still end in a chargeback, which would pay the Payer
# twice. Once withdrawn or expired, it no longer blocks (PRD #205).
_BLOCKING: frozenset[DisputeStatus] = frozenset({"open", "in_review", "ready"})


def refund_refusal(
    payment: Payment,
    dispute: DisputeStatus | None,
    earlier: list[Refund],
    amount: Money,
    window: timedelta,
    now: datetime,
    window_ended_at: datetime | None = None,
) -> RefundRefusal | None:
    """Why `amount` can't be refunded on `payment` at `now`, or None if it can. Only money
    that moved is returned: a scheduled Payment is cancelled instead, which is a Void.
    `dispute` is the status of the Payment's Dispute, if it has one. `window` is the active
    Policy pack's Refund window, counted from capture; `window_ended_at` is when a Demo
    control ended it early (ADR-0032), if one did. Pending Refunds count with processed
    ones, so two in flight can't overshoot; failed ones don't."""
    if payment.status != "captured":
        return "PAYMENT_NOT_REFUNDABLE"
    if dispute in _BLOCKING:
        return "PAYMENT_DISPUTED"
    # A captured Payment never changes again, so it was last updated when captured.
    if now > payment.updated_at + window:
        return "REFUND_WINDOW_CLOSED"
    if window_ended_at is not None and now >= window_ended_at:
        return "REFUND_WINDOW_CLOSED"
    held = sum(r.amount.minor for r in earlier if r.status != "failed")
    if held + amount.minor > payment.amount.minor:
        return "REFUND_EXCEEDS_CAPTURED"
    return None


class RefundReport(BaseModel):
    """What a rail event says about one of our Refunds, read from the rail's own format by
    its adapter. The ids come from the notes we set when the refund was made."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str
    refund_id: RefundId
    payment_id: PaymentId
    tenant_id: TenantId
    rail_refund_id: str
    status: Literal["processed", "failed"]
    amount: Money


def reason_hash(reason: str | None) -> str | None:
    """What the Audit chain holds of a Refund's reason: its SHA-256, never the words."""
    if reason is None:
        return None
    return "sha256:" + hashlib.sha256(reason.encode("utf-8")).hexdigest()
