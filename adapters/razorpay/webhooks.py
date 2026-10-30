"""Razorpay webhooks: checking the signature and reading payment events.

Razorpay signs the raw request body with HMAC-SHA256 and the webhook secret, hex encoded,
in `X-Razorpay-Signature`. `payment.captured` and `payment.failed` carry only the payment
entity, not the order, so our Payment id travels in the payment's `notes` (#81).
"""

import hashlib
import hmac
import json
from typing import Any

from pydantic import ValidationError

from core.domain.money import Money
from core.domain.payments import PaymentId, RailReport, SettledStatus

# The notes key the Razorpay adapter sets to our Payment id when it creates a payment.
PAYMENT_ID_NOTE = "agentpay_payment_id"

_SETTLED: dict[str, SettledStatus] = {"payment.captured": "captured", "payment.failed": "failed"}


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
