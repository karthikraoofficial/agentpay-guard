"""Recording Payments and what the rail says about their Rail charges, in the caller's
transaction. Each status change is one `payment` Audit record in the Tenant's chain.
Shared by the payments API, and later by rail webhooks and reconciliation (ADR-0014)."""

from datetime import datetime

from core.domain.audit import AuditRecord, PaymentEvidence, seal
from core.domain.decisions import AuditRecordId
from core.domain.parties import new_id
from core.domain.payments import Payment, PaymentId
from core.ports import RailCharge, TenantTransaction


def record_payment(tx: TenantTransaction, payment: Payment, now: datetime) -> None:
    """Store a new Payment with its first Audit record. The caller holds the Mandate."""
    tx.add_payment(payment)
    _audit(tx, payment, now)


def record_charge(
    tx: TenantTransaction, payment_id: PaymentId, charge: RailCharge | None, now: datetime
) -> Payment:
    """Apply what the rail reported to the Payment, forward only, auditing any change of
    status. `charge` is None when the rail's answer was unclear. Returns the Payment as it
    now is."""
    payment = tx.lock_payment(payment_id)
    if payment is None:
        raise RuntimeError(f"Payment {payment_id} vanished")
    if charge is None:
        return payment
    moved = payment.charged(charge.status, charge.rail_payment_id, charge.failure_code, now)
    if moved is None:
        return payment
    tx.update_payment(moved)
    if moved.status != payment.status:
        _audit(tx, moved, now)
    return moved


def _audit(tx: TenantTransaction, payment: Payment, now: datetime) -> None:
    seq, prev_hash = tx.lock_chain_head()
    tx.append_audit_record(
        seal(
            AuditRecord(
                audit_record_id=AuditRecordId(new_id("aud")),
                tenant_id=payment.tenant_id,
                seq=seq + 1,
                prev_hash=prev_hash,
                kind="payment",
                decision_id=payment.decision_id,
                recorded_at=now,
                payment=PaymentEvidence.of(payment),
            )
        )
    )
