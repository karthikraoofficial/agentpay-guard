"""The outbox relay: deliver outbox events (ADR-0009).

- `consent_requested` starts the Consent request's workflow. While the workflow side is
  down the event waits and is retried on every pass. Starting a workflow twice is a no-op
  (its ID is the Consent request ID), so a pass that dies after starting but before
  marking just repeats the start.
- Tenant webhook events go to the Tenant's endpoint, at least once. A failed send is
  retried with backoff, and given up a day after the event; a Tenant with no endpoint
  gets nothing sent.

An event is marked delivered only after its delivery succeeds. Workflow events and Tenant
webhook events are read in separate batches, so a backlog of one never holds up the other.
Tenant webhooks carry no ordering guarantee: a retried event can arrive after a later one.
"""

import logging
from datetime import datetime

from core.domain.consent import ConsentRequestId
from core.domain.webhooks import GIVE_UP_AFTER, TENANT_EVENT_TYPES, retry_at
from core.ports import Clock, DomainEvent, EventBus, Workflow
from core.tenant_webhooks import TenantWebhooks

log = logging.getLogger(__name__)

WORKFLOW_EVENT_TYPES = frozenset({"consent_requested"})


def relay_once(
    bus: EventBus,
    workflow: Workflow,
    clock: Clock,
    webhooks: TenantWebhooks,
    limit: int = 100,
) -> int:
    """Deliver up to `limit` due workflow events and up to `limit` due Tenant webhook
    events, oldest first in each. Returns how many were delivered."""
    delivered = 0
    for kinds in (WORKFLOW_EVENT_TYPES, TENANT_EVENT_TYPES):
        for event in bus.undelivered(limit, clock.now(), kinds):
            try:
                if event.event_type in TENANT_EVENT_TYPES:
                    webhooks.deliver(event)
                else:
                    _start_workflow(event, workflow)
            except Exception as exc:  # any failure leaves the event for a later pass
                _failed(bus, event, exc, clock.now())
                continue
            bus.mark_delivered(event.tenant_id, event.event_id, clock.now())
            delivered += 1
    return delivered


def _failed(bus: EventBus, event: DomainEvent, exc: Exception, now: datetime) -> None:
    reason = type(exc).__name__
    if event.event_type not in TENANT_EVENT_TYPES:
        log.warning(
            "outbox event %s for Tenant %s not delivered: %s",
            event.event_id,
            event.tenant_id,
            reason,
        )
        return
    if now >= event.occurred_at + GIVE_UP_AFTER:
        bus.give_up(event.tenant_id, event.event_id, now)
        log.error(
            "gave up on Tenant webhook %s (%s) for Tenant %s after %d attempts: %s",
            event.event_id,
            event.event_type,
            event.tenant_id,
            event.attempts + 1,
            reason,
        )
        return
    bus.retry_later(
        event.tenant_id, event.event_id, retry_at(event.attempts + 1, now, event.occurred_at)
    )
    log.warning(
        "Tenant webhook %s for Tenant %s not delivered, will retry: %s",
        event.event_id,
        event.tenant_id,
        reason,
    )


def _start_workflow(event: DomainEvent, workflow: Workflow) -> None:
    if event.event_type == "consent_requested":
        workflow.start_consent(
            event.tenant_id,
            ConsentRequestId(str(event.payload["consent_request_id"])),
            datetime.fromisoformat(str(event.payload["expires_at"])),
        )
