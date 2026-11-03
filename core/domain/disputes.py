"""Disputes: a Tenant's report that the Payer has challenged a captured Payment (see
GLOSSARY.md). A Payment has at most one Dispute, and opening one never changes the
Payment, its Decision or Spent this month."""

import hashlib
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

# open: the Evidence pack is being built. in_review: built, waiting for Sign-off.
DisputeStatus = Literal["open", "in_review", "ready", "expired", "withdrawn"]

DisputeRefusal = Literal["PAYMENT_NOT_DISPUTABLE", "PAYMENT_ALREADY_DISPUTED"]

SignOffRefusal = Literal["DISPUTE_NOT_IN_REVIEW", "PACK_VERSION_NOT_CURRENT"]

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


def statement_hash(statement: str | None) -> str | None:
    if statement is None:
        return None
    return "sha256:" + hashlib.sha256(statement.encode("utf-8")).hexdigest()
