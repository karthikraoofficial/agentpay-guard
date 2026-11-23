"""Demo controls (ADR-0032): the hosted demo ends a timer early, so a visitor sees an expiry
journey in seconds. A control is its own append-only record and its own Audit record. It
never moves a clock and never edits a Mandate, Decision or Audit record: each deadline
check reads the real deadline or the control's time, whichever comes first, and only for
the one configured Demo Tenant on the mock rail.

- `expire_consent`: the Consent request's TTL ends now. The control runs the real expire
  transition, so the request resolves `expired` with `resolved_via = demo_control`.
- `expire_payment_window`: the Decision's Payment window ends now.
- `expire_notice`: the Payment's Pre-debit notice period ends now; the worker's debit step
  then debits it, or fails it if the Mandate no longer allows it.
"""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from core.domain.decisions import AuditRecordId, DecisionId
from core.domain.parties import TenantId

DemoControlKind = Literal["expire_consent", "expire_payment_window", "expire_notice"]


class DemoControl(BaseModel):
    """A Demo control as stored for lookup beside its Audit record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    control_id: str
    tenant_id: TenantId
    kind: DemoControlKind
    # What it ended: a Consent request, Decision or Payment id, per `kind`.
    target_id: str
    # The Decision it concerns, so its Audit record sits in that Decision's trail.
    decision_id: DecisionId
    at: AwareDatetime
    audit_record_id: AuditRecordId


def ends(deadline: datetime, control: DemoControl | None) -> datetime:
    """The deadline as a Demo control leaves it: the real one, or the control's time if
    that came first. With no control, the real deadline."""
    if control is None:
        return deadline
    return min(deadline, control.at)
