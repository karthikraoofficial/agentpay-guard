"""A Dispute's life, in the caller's transaction (see GLOSSARY.md): opening it, signing
off its Evidence pack, withdrawing it, asking for its pack again, and expiring it.

Postgres owns Dispute state. Every change the `dispute_pack` workflow must hear about is
published to the outbox in the same transaction, so a nudge is never lost.

The Dispute is stored with its `dispute_opened` Audit record and an outbox event that
starts the workflow building its Evidence pack, so all three commit or none do. Opening a
Dispute never changes the Payment, its Decision or Spent this month."""

from datetime import datetime

from core.domain.audit import AuditRecord, DisputeEvidence, SignOffEvidence, seal
from core.domain.decisions import AuditRecordId, decision_time
from core.domain.disputes import (
    CLOSED,
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
from core.domain.webhooks import utc_iso
from core.ports import Clock, DomainEvent, EventType, TenantId, TenantTransaction, UnitOfWork


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
            payload={"dispute_id": dispute.dispute_id, "respond_by": utc_iso(respond_by)},
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
    _nudge(tx, ready, "dispute_closed", now)
    return ready


def withdraw(tx: TenantTransaction, dispute: Dispute, now: datetime) -> Dispute:
    """Close a Dispute the Tenant no longer needs, such as one the Payer dropped. A build
    in progress then writes nothing."""
    withdrawn = dispute.model_copy(update={"status": "withdrawn", "updated_at": now})
    tx.update_dispute(withdrawn)
    _nudge(tx, withdrawn, "dispute_closed", now)
    return withdrawn


def request_regenerate(tx: TenantTransaction, dispute: Dispute, now: datetime) -> Dispute:
    """Ask for the next pack version. The Dispute is open again until it is built, and that
    version needs its own Sign-off."""
    rebuilding = dispute.model_copy(update={"status": "open", "updated_at": now})
    tx.update_dispute(rebuilding)
    _nudge(tx, rebuilding, "dispute_regenerate", now)
    return rebuilding


class NotYetDue(Exception):
    """The Dispute's `respond_by` hasn't passed by this clock; try again later."""


def expire_dispute(
    store: UnitOfWork, tenant_id: TenantId, dispute_id: DisputeId, clock: Clock
) -> str:
    """Expire a Dispute nobody signed off by `respond_by`. Safe to run more than once:
    returns `expired`, `already_closed` or `missing`, and raises NotYetDue early."""
    now = decision_time(clock.now())
    with store.transaction(tenant_id) as tx:
        dispute = tx.lock_dispute(dispute_id)
        if dispute is None:
            return "missing"
        if dispute.status in CLOSED:
            return "already_closed"
        if now < dispute.respond_by:
            raise NotYetDue(f"Dispute {dispute_id} is not due yet")
        tx.update_dispute(dispute.model_copy(update={"status": "expired", "updated_at": now}))
    return "expired"


def _nudge(tx: TenantTransaction, dispute: Dispute, event_type: EventType, now: datetime) -> None:
    tx.publish(
        DomainEvent(
            event_id=new_id("evt"),
            tenant_id=dispute.tenant_id,
            event_type=event_type,
            occurred_at=now,
            payload={"dispute_id": dispute.dispute_id},
        )
    )
