"""Consent requests and their Consent resolutions (ADR-0008, ADR-0009).

A Consent request is a pending ask to the Payer to approve one step_up Decision. It ends
in at most one Consent resolution, recorded as a new Audit record and never by changing
the Decision. Until then its status is worked out from the time it is read.
"""

from datetime import datetime
from typing import Annotated, Literal, NewType

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints

from core.domain.decisions import AuditRecordId, DecisionId
from core.domain.mandates import ConsentMethod
from core.domain.parties import TenantId

ConsentRequestId = NewType("ConsentRequestId", str)
ResolutionStatus = Literal["approved", "declined", "expired", "blocked"]
ConsentStatus = Literal["pending", "approved", "declined", "expired", "blocked"]
# How a resolution arrived: the Tenant's API, a Consent link, or the expiry timer.
ResolvedVia = Literal["tenant_api", "consent_link", "timer"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Attestation(_Frozen):
    """How the Tenant confirmed it was the Payer who answered. Attested by the Tenant,
    not verified (ADR-0008)."""

    method: ConsentMethod
    reference: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    captured_at: AwareDatetime


class ResolutionEvidence(_Frozen):
    """What an Audit record of kind consent_resolution says about the resolution. The
    Decision ID and time are the record's own `decision_id` and `recorded_at`."""

    consent_request_id: ConsentRequestId
    status: ResolutionStatus
    resolved_via: ResolvedVia
    attestation: Attestation | None = None


class ConsentResolution(_Frozen):
    """How one Consent request ended, as stored for lookup beside its Audit record."""

    consent_request_id: ConsentRequestId
    tenant_id: TenantId
    decision_id: DecisionId
    status: ResolutionStatus
    resolved_at: AwareDatetime
    resolved_via: ResolvedVia
    attestation: Attestation | None
    audit_record_id: AuditRecordId


class ConsentRequest(_Frozen):
    consent_request_id: ConsentRequestId
    tenant_id: TenantId
    decision_id: DecisionId
    created_at: AwareDatetime
    expires_at: AwareDatetime
    # Loaded with the request when it has been resolved; never set at creation.
    resolution: ConsentResolution | None = None

    def status(self, now: datetime) -> ConsentStatus:
        """The resolution if there is one; otherwise expiry, worked out when read."""
        if self.resolution is not None:
            return self.resolution.status
        return "expired" if now >= self.expires_at else "pending"
