"""Mandates: a Payer's standing permission for named Agents to spend (ADR-0001, ADR-0005).

A Mandate's terms never change. Revocation is the only change, and it is final.
Status is computed from the time it is read, so expiry is never stored.
"""

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, NewType, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)

from core.domain.money import CurrencyCode, Money
from core.domain.parties import AgentId, PayerId, TenantId
from core.domain.payments import RailToken

MandateId = NewType("MandateId", str)
Mcc = Annotated[str, StringConstraints(pattern=r"^\d{4}$", strict=True)]
ConsentMethod = Literal["in_app_confirm", "otp", "upi_mandate_ref", "card_3ds_ref"]
RevokedBy = Literal["payer", "tenant"]
MandateStatus = Literal["active", "revoked", "expired", "not_yet_valid"]


def _unique[T](items: tuple[T, ...]) -> tuple[T, ...]:
    if len(set(items)) != len(items):
        raise ValueError("items must be unique")
    return items


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConsentEvidence(_Frozen):
    """How and when the Payer agreed. Attested by the Tenant, not verified (ADR-0005)."""

    method: ConsentMethod
    reference: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    captured_at: AwareDatetime


class MandateTerms(_Frozen):
    """What a Payer agreed to. Checked whenever terms are built, so an inconsistent
    Mandate can't exist anywhere in the system."""

    payer_id: PayerId
    allowed_agent_ids: Annotated[tuple[AgentId, ...], Field(min_length=1), AfterValidator(_unique)]
    currency: CurrencyCode
    per_txn_cap: Money
    monthly_cap: Money
    allowed_mccs: Annotated[tuple[Mcc, ...], Field(min_length=1), AfterValidator(_unique)]
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    supersedes: MandateId | None = None
    consent_evidence: ConsentEvidence
    # The instrument Payments under this Mandate charge. Optional: a Mandate without one
    # can be decided on but never paid (#74).
    rail_token: RailToken | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if {self.per_txn_cap.currency, self.monthly_cap.currency} != {self.currency}:
            raise ValueError("both caps must be in the Mandate's currency")
        if self.per_txn_cap.minor <= 0:
            raise ValueError("per_txn_cap must be positive")
        if self.monthly_cap.minor < self.per_txn_cap.minor:
            raise ValueError("monthly_cap must be at least per_txn_cap")
        if self.expires_at <= self.valid_from:
            raise ValueError("expires_at must be after valid_from")
        if self.expires_at > _one_year_after(self.valid_from):
            raise ValueError("a Mandate can last at most one year")
        if self.rail_token and self.rail_token.rail == "razorpay" and self.currency != "INR":
            raise ValueError("a Razorpay Rail token needs an INR Mandate")
        return self

    @model_serializer(mode="wrap")
    def _omit_missing_rail_token(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Leave `rail_token` out when there is none, so a Mandate snapshot in an Audit record
        keeps the exact bytes it had before Rail tokens existed."""
        data: dict[str, Any] = handler(self)
        if data.get("rail_token", ...) is None:
            del data["rail_token"]
        return data


class Mandate(MandateTerms):
    mandate_id: MandateId
    tenant_id: TenantId
    created_at: AwareDatetime
    revoked_by: RevokedBy | None = None
    revoked_at: AwareDatetime | None = None

    def status(self, now: datetime) -> MandateStatus:
        if self.revoked_at is not None:
            return "revoked"
        if now < self.valid_from:
            return "not_yet_valid"
        if now >= self.expires_at:
            return "expired"
        return "active"


def _one_year_after(at: datetime) -> datetime:
    try:
        return at.replace(year=at.year + 1)
    except ValueError:  # 29 February
        return at.replace(year=at.year + 1, day=28) + timedelta(days=1)
