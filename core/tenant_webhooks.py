"""Sending Tenant webhook events (see core/domain/webhooks.py for the signature scheme).

The outbox relay hands each Tenant webhook event here. Delivery is at least once: a Tenant
dedupes by the event id, which is in the body and the `X-AgentPay-Event-Id` header.
"""

from dataclasses import dataclass

from core.domain.webhooks import EVENT_ID_HEADER, SIGNATURE_HEADER, body, signature
from core.ports import Clock, DomainEvent, SecretBox, UnitOfWork, WebhookSender


class WebhookRefused(Exception):
    """The endpoint answered, but not with a 2xx."""


@dataclass(frozen=True)
class TenantWebhooks:
    store: UnitOfWork
    box: SecretBox
    sender: WebhookSender
    clock: Clock

    def deliver(self, event: DomainEvent) -> bool:
        """Send the event to its Tenant's endpoint. False when the Tenant has none, so
        nothing is sent. Raises `WebhookUnreachable` or `WebhookRefused` on a failed send."""
        with self.store.transaction(event.tenant_id) as tx:
            endpoint = tx.get_webhook_endpoint()
        if endpoint is None:
            return False
        raw = body(event.event_id, event.event_type, event.occurred_at, event.payload)
        signed = signature(
            self.box.decrypt(endpoint.secret_ciphertext), int(self.clock.now().timestamp()), raw
        )
        status = self.sender.post(
            endpoint.url,
            raw,
            {
                "Content-Type": "application/json",
                SIGNATURE_HEADER: signed,
                EVENT_ID_HEADER: event.event_id,
            },
        )
        if not 200 <= status < 300:
            raise WebhookRefused(f"HTTP {status}")
        return True
