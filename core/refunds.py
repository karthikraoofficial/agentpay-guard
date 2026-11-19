"""A Refund's life, in the caller's transaction (PRD #205, ADR-0024): storing it with its
first `refund` Audit record, then applying the rail's answer with the second.

Like a Payment (ADR-0014), the rail is asked between two transactions: the Refund is
stored `pending` first, so a crash or a timeout after the rail call leaves a Refund that
can be asked about again by its id, never one the rail made that we don't know of."""

from datetime import datetime

from core.domain.audit import AuditRecord, RefundEvidence, seal
from core.domain.decisions import AuditRecordId, PackRef
from core.domain.money import Money
from core.domain.parties import new_id
from core.domain.payments import Payment
from core.domain.refunds import Refund, RefundId
from core.ports import RailRefund, TenantTransaction


def request_refund(
    tx: TenantTransaction,
    payment: Payment,
    amount: Money,
    reason: str | None,
    pack: PackRef,
    now: datetime,
) -> Refund:
    """Store a pending Refund on a Payment the caller holds and has checked."""
    refund = Refund(
        refund_id=RefundId(new_id("rfd")),
        tenant_id=payment.tenant_id,
        payment_id=payment.payment_id,
        decision_id=payment.decision_id,
        mandate_id=payment.mandate_id,
        amount=amount,
        reason=reason,
        status="pending",
        pack=pack,
        created_at=now,
        updated_at=now,
    )
    tx.add_refund(refund)
    _audit(tx, refund, now)
    return refund


def record_refund(
    tx: TenantTransaction, refund_id: RefundId, answer: RailRefund | None, now: datetime
) -> Refund:
    """Apply what the rail said about a Refund, forward only, auditing any change of
    status. `answer` is None when the rail's answer was unclear. Returns the Refund as it
    now is."""
    refund = tx.lock_refund(refund_id)
    if refund is None:
        raise RuntimeError(f"Refund {refund_id} vanished")
    if answer is None:
        return refund
    moved = refund.settled(answer.status, answer.rail_refund_id, answer.failure_code, now)
    if moved is None:
        return refund
    tx.update_refund(moved)
    _audit(tx, moved, now)
    return moved


def _audit(tx: TenantTransaction, refund: Refund, now: datetime) -> None:
    seq, prev_hash = tx.lock_chain_head()
    tx.append_audit_record(
        seal(
            AuditRecord(
                audit_record_id=AuditRecordId(new_id("aud")),
                tenant_id=refund.tenant_id,
                seq=seq + 1,
                prev_hash=prev_hash,
                kind="refund",
                decision_id=refund.decision_id,
                recorded_at=now,
                refund=RefundEvidence.of(refund),
            )
        )
    )
