"""Disputes: a Tenant's report that the Payer has challenged a captured Payment (see
GLOSSARY.md). A Payment has at most one Dispute, and opening one never changes the
Payment, its Decision or Spent this month."""

import hashlib
from datetime import datetime
from typing import Annotated, Literal, NewType

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from core.domain.decisions import DecisionId
from core.domain.parties import TenantId
from core.domain.payments import PaymentId

DisputeId = NewType("DisputeId", str)

DisputeReason = Literal[
    "unauthorised",
    "not_as_described",
    "not_received",
    "duplicate",
    "amount_differs",
    "cancelled_recurring",
]

# Facts about an Evidence bundle that fixed code works out and the Policy pack weighs per
# Dispute reason (ADR-0002): which make the evidence strong, and which make it weak.
EvidenceFact = Literal[
    "decision_within_mandate",  # allowed, or a step-up the Payer approved
    "payer_intent_confirmed",  # an Intent match, or an approval with an Attestation
    "intent_mismatch_approved",  # the Payer approved a step-up whose Intent check said mismatch
    "mandate_revoked_before_capture",  # revoked after the Decision, before the capture
    "mandate_active_at_decision",
    "mandate_revoked_before_decision",
    "amount_matches_decision",  # the captured amount is the Decision's
    "amount_differs_from_decision",
    "within_quoted_total",  # at or below the Quoted total of the Intent check cited
    "no_duplicate_payment",
    "duplicate_payment_found",  # same Payer, merchant and amount captured within the window
    "fulfilment_evidence",  # never in a bundle: only the merchant holds it
]

EvidenceStrength = Literal["strong", "mixed", "weak"]

# open: the Evidence pack is being built. in_review: built, waiting for Sign-off.
DisputeStatus = Literal["open", "in_review", "ready", "expired", "withdrawn"]

DisputeRefusal = Literal["PAYMENT_NOT_DISPUTABLE", "PAYMENT_ALREADY_DISPUTED"]

SignOffRefusal = Literal["DISPUTE_NOT_IN_REVIEW", "PACK_VERSION_NOT_CURRENT"]

LifecycleRefusal = Literal["DISPUTE_CLOSED", "DISPUTE_NOT_IN_REVIEW", "PACK_VERSION_LIMIT"]

# A closed Dispute changes no more: signed off, past its deadline, or withdrawn.
CLOSED: frozenset[DisputeStatus] = frozenset({"ready", "expired", "withdrawn"})

_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

PAYER_STATEMENT_MAX = 2000


class Reviewer(BaseModel):
    """The person at the Tenant who signs off a pack. The Tenant attests who it was;
    AgentPay Guard does not verify it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: _Name
    name: _Name


class SignOff(BaseModel):
    """A named person approving one exact pack version, identified by its pack hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer: Reviewer
    pack_version: int = Field(ge=1)
    pack_hash: str
    signed_off_at: AwareDatetime


class Dispute(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dispute_id: DisputeId
    tenant_id: TenantId
    payment_id: PaymentId
    decision_id: DecisionId
    reason: DisputeReason
    respond_by: AwareDatetime
    # The Payer's own words, kept beside the Dispute and never in the Audit chain, which
    # holds only its hash (as with the Payer instruction, ADR-0007).
    payer_statement: str | None = Field(default=None, max_length=PAYER_STATEMENT_MAX)
    status: DisputeStatus
    # The seq of the Dispute's `dispute_opened` Audit record. The Evidence bundle reads the
    # chain only up to here, so it is the same however much the chain grows later.
    opened_seq: int = Field(ge=1)
    # Set once, by Sign-off, which makes the Dispute ready.
    sign_off: SignOff | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ReviewLink(BaseModel):
    """An expiring link to one Dispute's Evidence pack for a named reviewer (see
    GLOSSARY.md). Only the token's hash is kept. It works until a newer link replaces it,
    Sign-off uses it up, it expires, or the Dispute is closed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_hash: str
    tenant_id: TenantId
    dispute_id: DisputeId
    reviewer: Reviewer
    created_at: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    used_at: AwareDatetime | None = None

    def works(self, status: DisputeStatus, now: datetime) -> bool:
        return (
            self.revoked_at is None
            and self.used_at is None
            and now < self.expires_at
            and status not in CLOSED
        )


def sign_off_refusal(
    status: DisputeStatus, current_version: int | None, pack_version: int
) -> SignOffRefusal | None:
    """Why a pack version can't be signed off, or None if it can: only the latest pack of
    a Dispute in review."""
    if status != "in_review" or current_version is None:
        return "DISPUTE_NOT_IN_REVIEW"
    if pack_version != current_version:
        return "PACK_VERSION_NOT_CURRENT"
    return None


def withdraw_refusal(status: DisputeStatus) -> LifecycleRefusal | None:
    return "DISPUTE_CLOSED" if status in CLOSED else None


def regenerate_refusal(
    status: DisputeStatus, current_version: int | None, versions_max: int
) -> LifecycleRefusal | None:
    """Why a pack can't be built again, or None if it can: only from review, so a build in
    progress is never doubled, and only up to `versions_max` versions."""
    if status in CLOSED:
        return "DISPUTE_CLOSED"
    if status != "in_review" or current_version is None:
        return "DISPUTE_NOT_IN_REVIEW"
    if current_version >= versions_max:
        return "PACK_VERSION_LIMIT"
    return None


def statement_hash(statement: str | None) -> str | None:
    if statement is None:
        return None
    return "sha256:" + hashlib.sha256(statement.encode("utf-8")).hexdigest()
