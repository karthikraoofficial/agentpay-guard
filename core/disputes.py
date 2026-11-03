"""Opening a Dispute and signing off its Evidence pack, in the caller's transaction (see
GLOSSARY.md).

The Dispute is stored with its `dispute_opened` Audit record and an outbox event that
starts the workflow building its Evidence pack, so all three commit or none do. Opening a
Dispute never changes the Payment, its Decision or Spent this month."""

from datetime import datetime

from core.domain.audit import AuditRecord, DisputeEvidence, SignOffEvidence, seal
from core.domain.decisions import AuditRecordId
from core.domain.disputes import (
    Dispute,
    DisputeId,
    DisputeReason,
    DisputeRefusal,
    Reviewer,
    SignOff,
    statement_hash,
)
from core.domain.evidence import EvidencePack
from core.domain.parties import new_id
from core.domain.payments import Payment, PaymentStatus
from core.ports import DomainEvent, TenantTransaction


def dispute_refusal(status: PaymentStatus, already_disputed: bool) -> DisputeRefusal | None:
    """Why a Payment can't be disputed, or None if it can. Only money that moved can be
    disputed, and only once."""
    if status != "captured":
        return "PAYMENT_NOT_DISPUTABLE"
    if already_disputed:
        return "PAYMENT_ALREADY_DISPUTED"
    return None


def open_dispute(
    tx: TenantTransaction,
    payment: Payment,
    reason: DisputeReason,
    respond_by: datetime,
    payer_statement: str | None,
    now: datetime,
) -> Dispute:
    """Store a Dispute on a Payment the caller holds and has checked with
    `dispute_refusal`."""
    seq, prev_hash = tx.lock_chain_head()
    dispute = Dispute(
        dispute_id=DisputeId(new_id("dsp")),
        tenant_id=payment.tenant_id,
        payment_id=payment.payment_id,
        decision_id=payment.decision_id,
        reason=reason,
        respond_by=respond_by,
        payer_statement=payer_statement,
        status="open",
        opened_seq=seq + 1,
        created_at=now,
        updated_at=now,
    )
    tx.add_dispute(dispute)
    tx.append_audit_record(
        seal(
            AuditRecord(
                audit_record_id=AuditRecordId(new_id("aud")),
                tenant_id=payment.tenant_id,
                seq=seq + 1,
                prev_hash=prev_hash,
                kind="dispute_opened",
                decision_id=payment.decision_id,
                recorded_at=now,
                dispute=DisputeEvidence(
                    dispute_id=dispute.dispute_id,
                    payment_id=payment.payment_id,
                    reason=reason,
                    respond_by=respond_by,
                    payer_statement_hash=statement_hash(payer_statement),
                ),
            )
        )
    )
    tx.publish(
        DomainEvent(
            event_id=new_id("evt"),
            tenant_id=payment.tenant_id,
            event_type="dispute_opened",
            occurred_at=now,
            payload={"dispute_id": dispute.dispute_id},
        )
    )
    return dispute


def sign_off(
    tx: TenantTransaction, dispute: Dispute, pack: EvidencePack, reviewer: Reviewer, now: datetime
) -> Dispute:
    """Approve one pack version of a Dispute the caller holds and has checked with
    `sign_off_refusal`. The pack hash goes into the Audit chain, which fixes what was
    approved, and the Dispute becomes ready."""
    pack_hash = pack.hash()
    seq, prev_hash = tx.lock_chain_head()
    tx.append_audit_record(
        seal(
            AuditRecord(
                audit_record_id=AuditRecordId(new_id("aud")),
                tenant_id=dispute.tenant_id,
                seq=seq + 1,
                prev_hash=prev_hash,
                kind="dispute_signed_off",
                decision_id=dispute.decision_id,
                recorded_at=now,
                sign_off=SignOffEvidence(
                    dispute_id=dispute.dispute_id,
                    pack_version=pack.version,
                    pack_hash=pack_hash,
                    reviewer=reviewer,
                ),
            )
        )
    )
    ready = dispute.model_copy(
        update={
            "status": "ready",
            "sign_off": SignOff(
                reviewer=reviewer,
                pack_version=pack.version,
                pack_hash=pack_hash,
                signed_off_at=now,
            ),
            "updated_at": now,
        }
    )
    tx.update_dispute(ready)
    return ready
