"""Tenant webhooks: signed messages we send to a Tenant's endpoint when something it
cares about happens, so it doesn't have to poll (see GLOSSARY.md).

Every body holds identifiers, statuses, codes and times only: never a Payer's contact
details, a Rail token's refs or free text. The signature scheme is the one Tenants verify:

    X-AgentPay-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<body>">

keyed with the endpoint's signing secret, which is shown to the Tenant once and stored
encrypted (ADR-0015).
"""

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints

from core.domain.parties import TenantId

SIGNATURE_HEADER = "X-AgentPay-Signature"
EVENT_ID_HEADER = "X-AgentPay-Event-Id"

# The events a Tenant webhook carries.
TenantEventType = Literal[
    "consent_request.created",
    "consent_request.resolved",
    "payment.captured",
    "payment.failed",
]
TENANT_EVENT_TYPES: frozenset[str] = frozenset(TenantEventType.__args__)  # type: ignore[attr-defined]

# Delivery is at least once: a failed send is retried with backoff, doubling from 30 s up
# to an hour, until a day after the event, then given up.
GIVE_UP_AFTER = timedelta(hours=24)
_FIRST_RETRY = timedelta(seconds=30)
_LONGEST_WAIT = timedelta(hours=1)

_HttpsUrl = Annotated[
    str, StringConstraints(max_length=2048, pattern=r"^https://[^\s/?#]+(/[^\s#]*)?$")
]


class WebhookEndpoint(BaseModel):
    """A Tenant's one webhook endpoint. `secret_ciphertext` is the signing secret
    encrypted with the key from settings, never the secret itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    url: _HttpsUrl
    secret_ciphertext: str
    created_at: AwareDatetime
    secret_rotated_at: AwareDatetime


def new_signing_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"


def signature(secret: str, timestamp: int, body: bytes) -> str:
    """The `X-AgentPay-Signature` value for `body`, sent at `timestamp` (unix seconds)."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def body(
    event_id: str, event_type: str, occurred_at: datetime, data: Mapping[str, object]
) -> bytes:
    """The JSON body sent for an event, the same bytes on every attempt."""
    envelope = {
        "id": event_id,
        "type": event_type,
        "created_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "data": dict(data),
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()


def retry_at(attempts: int, now: datetime) -> datetime:
    """When to try again after the `attempts`-th failed send (1 for the first)."""
    wait: timedelta = min(_FIRST_RETRY * (2 ** min(attempts - 1, 10)), _LONGEST_WAIT)
    return now + wait
