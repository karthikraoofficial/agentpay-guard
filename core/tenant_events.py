"""The Tenant webhook events, built for the outbox. Each is published in the transaction
that makes its change, so a rolled-back change sends nothing. Bodies hold identifiers,
statuses, codes, amounts in minor units and times only."""

from datetime import datetime

from core.domain.consent import ConsentRequest, ConsentResolution
from core.domain.parties import new_id
from core.domain.payments import Payment
from core.ports import DomainEvent


def consent_request_created(consent: ConsentRequest) -> DomainEvent:
    return DomainEvent(
        event_id=new_id("evt"),
        tenant_id=consent.tenant_id,
        event_type="consent_request.created",
        occurred_at=consent.created_at,
        payload={
            "consent_request_id": consent.consent_request_id,
            "decision_id": consent.decision_id,
            "status": "pending",
            "expires_at": _iso(consent.expires_at),
        },
    )


def consent_request_resolved(resolution: ConsentResolution) -> DomainEvent:
    return DomainEvent(
        event_id=new_id("evt"),
        tenant_id=resolution.tenant_id,
        event_type="consent_request.resolved",
        occurred_at=resolution.resolved_at,
        payload={
            "consent_request_id": resolution.consent_request_id,
            "decision_id": resolution.decision_id,
            "status": resolution.status,
            "resolved_via": resolution.resolved_via,
            "resolved_at": _iso(resolution.resolved_at),
        },
    )


def payment_settled(payment: Payment, now: datetime) -> DomainEvent:
    """`payment.captured` or `payment.failed`, for a Payment that just settled."""
    if payment.status == "scheduled":
        raise ValueError(f"Payment {payment.payment_id} hasn't settled")
    return DomainEvent(
        event_id=new_id("evt"),
        tenant_id=payment.tenant_id,
        event_type="payment.captured" if payment.status == "captured" else "payment.failed",
        occurred_at=now,
        payload={
            "payment_id": payment.payment_id,
            "decision_id": payment.decision_id,
            "mandate_id": payment.mandate_id,
            "status": payment.status,
            "amount_minor": payment.amount.minor,
            "currency": payment.amount.currency,
            "failure_code": payment.failure_code,
        },
    )


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")
