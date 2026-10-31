"""Recording Payments and what the rail says about their Rail charges, in the caller's
transaction. Each status change is one `payment` Audit record in the Tenant's chain, and
a failed Payment Voids its Decision. `apply_charge` is the one transition every report goes
through: the payments API, rail webhooks (#81) and the reconciliation sweep (#82)."""

import logging
from datetime import datetime

from core.domain.audit import AuditRecord, PaymentEvidence, seal
from core.domain.decisions import AuditRecordId
from core.domain.parties import new_id
from core.domain.payments import Payment, PaymentId
from core.ports import RailCharge, TenantTransaction
from core.tenant_events import payment_settled
from core.voids import record_void

log = logging.getLogger(__name__)


def record_payment(tx: TenantTransaction, payment: Payment, now: datetime) -> None:
    """Store a new Payment with its first Audit record. The caller holds the Mandate."""
    tx.add_payment(payment)
    _audit(tx, payment, now)


def apply_charge(
    tx: TenantTransaction, payment_id: PaymentId, charge: RailCharge | None, now: datetime
) -> Payment:
    """Apply a rail report to a Payment: forward only, auditing any change, and Voiding
    the Decision when the Payment fails so its amount leaves Spent this month. Locks the
    Mandate first, as Decisions, payments and Voids do, so reports about one Payment
    apply one after another."""
    payment = tx.get_payment(payment_id)
    if payment is None:
        raise RuntimeError(f"Payment {payment_id} vanished")
    tx.lock_mandate(payment.mandate_id)
    current = record_charge(tx, payment_id, charge, now)
    if current.status == "failed" and tx.get_void(current.decision_id) is None:
        record_void(tx, current.decision_id, "payment_failed", now, payment_id=payment_id)
    return current


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
    moved = payment.charged(
        charge.status, charge.rail_payment_id, charge.failure_code, now, charge.debit_after
    )
    if moved is None:
        if payment.status == "failed" and charge.status == "captured":
            # A settled Payment never reopens, but money moved: it needs a refund.
            log.error("Payment %s was captured at the rail after it failed", payment_id)
        return payment
    tx.update_payment(moved)
    if moved.status != payment.status:
        _audit(tx, moved, now)
        tx.publish(payment_settled(moved, now))
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
