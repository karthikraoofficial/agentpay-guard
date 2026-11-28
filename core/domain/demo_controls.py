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
- `expire_dispute`: the Dispute's response deadline passes now. The control runs the
  deadline's transition, so the Dispute is `expired`; `respond_by` is left as it was.
- `expire_refund_window`: the captured Payment's Refund window ends now, so a refund
  asked for after it is refused with `REFUND_WINDOW_CLOSED` (#285).

The switches force a failure, and run before any Decision, so they have no `decision_id`:

- `ai_off`: model calls for this Mandate fail closed.
- `rail_declining`: charges and refunds under this Mandate answer failed.
- `lose_payment`: charges under this Mandate never reach the rail.
- `rail_down`: the mock rail stops answering for the Demo Tenant, for a limited time.
"""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from core.domain.decisions import AuditRecordId, DecisionId
from core.domain.parties import TenantId

DemoControlKind = Literal[
    "expire_consent",
    "expire_payment_window",
    "expire_notice",
    "expire_dispute",
    "expire_refund_window",
    "ai_off",
    "rail_declining",
    "lose_payment",
    "rail_down",
]
# The target of `rail_down`, which is about the rail rather than one Mandate.
RAIL_TARGET = "rail"


class DemoControl(BaseModel):
    """A Demo control as stored for lookup beside its Audit record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    control_id: str
    tenant_id: TenantId
    kind: DemoControlKind
    # What it acts on: a Consent request, Decision, Payment, Dispute or Mandate id, or
    # RAIL_TARGET.
    target_id: str
    # The Decision it concerns, so its Audit record sits in that Decision's trail. A
    # switch has none.
    decision_id: DecisionId | None
    at: AwareDatetime
    audit_record_id: AuditRecordId


def ends(deadline: datetime, control: DemoControl | None) -> datetime:
    """The deadline as a Demo control leaves it: the real one, or the control's time if
    that came first. With no control, the real deadline."""
    if control is None:
        return deadline
    return min(deadline, control.at)
