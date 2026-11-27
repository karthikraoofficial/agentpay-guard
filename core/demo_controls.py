"""Demo controls (ADR-0032): ending one of the Demo Tenant's timers early, in the caller's
transaction. Each control is one `demo_controls` row plus one `demo_control` Audit record
in the Tenant's chain. Nothing is edited and no clock moves.

The gate is `control_for`: every deadline check that honours a control reads it through
that function, which answers None for any Tenant but the configured Demo Tenant. Settings
allow a Demo Tenant only on the mock rail, so no control can touch real money.
"""

from datetime import datetime

from core.disputes import expire_now
from core.domain.audit import AuditRecord, DemoControlEvidence, seal
from core.domain.consent import ConsentRequest
from core.domain.decisions import AuditRecordId, DecisionId
from core.domain.demo_controls import DemoControl, DemoControlKind
from core.domain.disputes import Dispute
from core.domain.parties import TenantId, new_id
from core.ports import TenantTransaction
from core.resolutions import record_resolution


def control_for(
    tx: TenantTransaction,
    demo_tenant: TenantId | None,
    kind: DemoControlKind,
    target_id: str,
) -> DemoControl | None:
    """The Demo control a deadline check honours: only ever the configured Demo Tenant's
    (`AGENTPAY_DEMO_CONTROLS_TENANT_ID`). For any other Tenant, or with none configured,
    None, whatever the table holds."""
    if demo_tenant is None or tx.tenant_id != demo_tenant:
        return None
    return tx.get_demo_control(kind, target_id)


def record_control(
    tx: TenantTransaction,
    kind: DemoControlKind,
    target_id: str,
    decision_id: DecisionId | None,
    now: datetime,
) -> DemoControl:
    """Append the control's Audit record and store it for lookup."""
    seq, prev_hash = tx.lock_chain_head()
    control = DemoControl(
        control_id=new_id("dctl"),
        tenant_id=tx.tenant_id,
        kind=kind,
        target_id=target_id,
        decision_id=decision_id,
        at=now,
        audit_record_id=AuditRecordId(new_id("aud")),
    )
    tx.append_audit_record(
        seal(
            AuditRecord(
                audit_record_id=control.audit_record_id,
                tenant_id=control.tenant_id,
                seq=seq + 1,
                prev_hash=prev_hash,
                kind="demo_control",
                decision_id=decision_id,
                recorded_at=now,
                demo_control=DemoControlEvidence(
                    control_id=control.control_id, control=kind, target_id=target_id
                ),
            )
        )
    )
    tx.add_demo_control(control)
    return control


def expire_consent_now(
    tx: TenantTransaction, consent: ConsentRequest, now: datetime
) -> DemoControl:
    """End an unresolved Consent request's TTL now: the control's record, then the same
    `expired` resolution the timer writes, with `resolved_via = demo_control`. The caller
    holds the request's lock and has checked it is unresolved."""
    control = record_control(
        tx, "expire_consent", consent.consent_request_id, consent.decision_id, now
    )
    record_resolution(tx, consent, "expired", "demo_control", None, now)
    return control


def expire_dispute_now(tx: TenantTransaction, dispute: Dispute, now: datetime) -> DemoControl:
    """End a Dispute's response deadline now: the control's record, then the same `expired`
    transition the deadline runs. `respond_by` is left as it was. The caller holds the
    Dispute's lock and has checked it isn't closed."""
    control = record_control(tx, "expire_dispute", dispute.dispute_id, dispute.decision_id, now)
    expire_now(tx, dispute, now)
    return control
