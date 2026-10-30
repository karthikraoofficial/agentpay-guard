"""Consent requests: a pending ask to the Payer to approve one step_up Decision."""

from datetime import datetime
from typing import Literal, NewType

from pydantic import AwareDatetime, BaseModel, ConfigDict

from core.domain.decisions import DecisionId
from core.domain.parties import TenantId

ConsentRequestId = NewType("ConsentRequestId", str)
# Only pending and expired are worked out today; approved, declined and blocked come
# from a Consent resolution.
ConsentStatus = Literal["pending", "approved", "declined", "expired", "blocked"]


class ConsentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    consent_request_id: ConsentRequestId
    tenant_id: TenantId
    decision_id: DecisionId
    created_at: AwareDatetime
    expires_at: AwareDatetime

    def status(self, now: datetime) -> ConsentStatus:
        """Expiry is worked out when read, never written."""
        return "expired" if now >= self.expires_at else "pending"
