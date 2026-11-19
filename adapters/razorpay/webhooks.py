"""Razorpay webhooks: checking the signature and reading payment and refund events.

Razorpay signs the raw request body with HMAC-SHA256 and the webhook secret, hex encoded,
in `X-Razorpay-Signature`. `payment.captured` and `payment.failed` carry only the payment
entity, not the order, so our Payment id travels in the payment's `notes` (#81).
`refund.processed` and `refund.failed` carry the refund entity, whose notes hold our
Refund, Payment and Tenant ids (#210).
"""

import hashlib
import hmac
import json
from typing import Any, Literal

from pydantic import ValidationError

from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId, RailReport, SettledStatus
from core.domain.refunds import RefundId, RefundReport

# The notes key the Razorpay adapter sets to our Payment id when it creates a payment.
PAYMENT_ID_NOTE = "agentpay_payment_id"

_SETTLED: dict[str, SettledStatus] = {"payment.captured": "captured", "payment.failed": "failed"}
_REFUNDED: dict[str, Literal["processed", "failed"]] = {
    "refund.processed": "processed",
    "refund.failed": "failed",
}
# The notes key that ties an order or refund to its Tenant: one rail account serves every
# Tenant, so a Tenant only ever sees what carries its own id (ADR-0012).
TENANT_NOTE = "agentpay_tenant_id"
# The notes key that ties a Razorpay refund to our Refund (#210).
REFUND_ID_NOTE = "agentpay_refund_id"


def signature_ok(raw: bytes, signature: str | None, secret: str | None) -> bool:
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def payment_report(raw: bytes) -> RailReport | None:
    """The report in a signed body, or None for an event we don't act on: another type,
    or a payment without our Payment id in its notes."""
    try:
        body = json.loads(raw)
        event_type = body["event"]
        status = _SETTLED.get(event_type)
        if status is None:
            return None
        entity: dict[str, Any] = body["payload"]["payment"]["entity"]
        notes = entity.get("notes")
        payment_id = notes.get(PAYMENT_ID_NOTE) if isinstance(notes, dict) else None
        if not payment_id:
            return None
        return RailReport(
            event_type=event_type,
            payment_id=PaymentId(payment_id),
            rail_payment_id=entity["id"],
            status=status,
            amount=Money(minor=entity["amount"], currency=entity["currency"]),
            failure_code=(entity.get("error_code") or None) if status == "failed" else None,
        )
    except (ValueError, KeyError, TypeError, AttributeError, ValidationError):
        return None


def refund_report(raw: bytes) -> RefundReport | None:
    """The refund report in a signed body, or None for an event we don't act on: another
    type, or a refund without our ids in its notes, such as one made in the dashboard."""
    try:
        body = json.loads(raw)
        event_type = body["event"]
        status = _REFUNDED.get(event_type)
        if status is None:
            return None
        entity: dict[str, Any] = body["payload"]["refund"]["entity"]
        found = entity.get("notes")
        notes: dict[str, Any] = found if isinstance(found, dict) else {}  # [] when empty
        refund_id, payment_id = notes.get(REFUND_ID_NOTE), notes.get(PAYMENT_ID_NOTE)
        tenant_id = notes.get(TENANT_NOTE)
        if not (refund_id and payment_id and tenant_id):
            return None
        return RefundReport(
            event_type=event_type,
            refund_id=RefundId(refund_id),
            payment_id=PaymentId(payment_id),
            tenant_id=TenantId(tenant_id),
            rail_refund_id=entity["id"],
            status=status,
            amount=Money(minor=entity["amount"], currency=entity["currency"]),
        )
    except (ValueError, KeyError, TypeError, AttributeError, ValidationError):
        return None
