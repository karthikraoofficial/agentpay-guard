"""The outbox relay: turn outbox events into workflow starts (ADR-0009).

An event is marked delivered only after its workflow start succeeds, so while the
workflow side is down every event waits and is retried on the next pass. Starting a
workflow twice is a no-op (its ID is the Consent request ID), so a pass that dies after
starting but before marking just repeats the start.
"""

import logging
from datetime import datetime

from core.domain.consent import ConsentRequestId
from core.ports import Clock, DomainEvent, EventBus, Workflow

log = logging.getLogger(__name__)


def relay_once(bus: EventBus, workflow: Workflow, clock: Clock, limit: int = 100) -> int:
    """Deliver up to `limit` waiting events, oldest first. Returns how many were delivered."""
    delivered = 0
    for event in bus.undelivered(limit):
        try:
            _deliver(event, workflow)
        except Exception as exc:  # any failure leaves the event for the next pass
            log.warning(
                "outbox event %s for Tenant %s not delivered: %s",
                event.event_id,
                event.tenant_id,
                type(exc).__name__,
            )
            continue
        bus.mark_delivered(event.tenant_id, event.event_id, clock.now())
        delivered += 1
    return delivered


def _deliver(event: DomainEvent, workflow: Workflow) -> None:
    match event.event_type:
        case "consent_requested":
            workflow.start_consent(
                event.tenant_id,
                ConsentRequestId(event.payload["consent_request_id"]),
                datetime.fromisoformat(event.payload["expires_at"]),
            )
