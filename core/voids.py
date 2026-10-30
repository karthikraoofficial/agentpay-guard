"""Writing a Void: one `void` Audit record in the Tenant's chain plus one row for lookup,
in the caller's transaction. Shared by the API (a Tenant voids an unpaid allow) and the
handling of a failed Payment, which voids its Decision."""

from datetime import datetime

from core.domain.audit import AuditRecord, VoidEvidence, seal
from core.domain.decisions import AuditRecordId, DecisionId
from core.domain.parties import new_id
from core.domain.payments import PaymentId
from core.domain.voids import Void, VoidVia
from core.ports import TenantTransaction


def record_void(
    tx: TenantTransaction,
    decision_id: DecisionId,
    via: VoidVia,
    now: datetime,
    *,
    payment_id: PaymentId | None = None,
) -> Void:
    """Void the Decision. The caller has checked it may be voided and holds its Mandate,
    so a Decision on the same Mandate can't count the spend while this commits."""
    seq, prev_hash = tx.lock_chain_head()
    void = Void(
        tenant_id=tx.tenant_id,
        decision_id=decision_id,
        via=via,
        payment_id=payment_id,
        voided_at=now,
        audit_record_id=AuditRecordId(new_id("aud")),
    )
    tx.append_audit_record(
        seal(
            AuditRecord(
                audit_record_id=void.audit_record_id,
                tenant_id=void.tenant_id,
                seq=seq + 1,
                prev_hash=prev_hash,
                kind="void",
                decision_id=decision_id,
                recorded_at=now,
                void=VoidEvidence(via=via, payment_id=payment_id),
            )
        )
    )
    tx.add_void(void)
    return void
