"""The webhook inbox (#286): where the Demo runner receives the Demo Tenant's Tenant
webhooks, so a visitor sees them arrive during a run (H8).

A delivery is kept only if its `X-AgentPay-Signature` checks out against the Demo Tenant's
signing secret (the scheme in core/domain/webhooks.py) and was made in the last five
minutes. Delivery is at least once, so an event is kept once, by its id. A run reads only
the events whose data names one of its own ids (its Mandate, Decision, Payment, Consent
request, Dispute or Refund), so one run never sees another's.

The inbox lives in the runner's memory and keeps only the newest events: a restart
empties it, as it ends every run. The secret never leaves this object: not into an
event, a Card or a log line.
"""

import hmac
import json
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from core.domain.webhooks import SIGNATURE_HEADER, TENANT_EVENT_TYPES, signature

# How old a signature may be: a replayed delivery from long ago is refused.
TOLERANCE = timedelta(minutes=5)
# Events kept, newest last: enough for every run in progress at once.
KEEP = 2000


class BadSignature(Exception):
    """The delivery isn't a Tenant webhook signed with the Demo Tenant's secret just now."""


class ReceivedEvent(BaseModel):
    """One Tenant webhook as the inbox kept it: only deliveries that verified are kept."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    event_type: str
    created_at: str
    data: dict[str, Any]
    # When the API signed it, from the signature's `t=`.
    signed_at: datetime
    signature: Literal["verified"] = "verified"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _signed_at(value: str | None) -> tuple[int, str]:
    """The `t` and `v1` parts of an `X-AgentPay-Signature` value."""
    if value is None:
        raise BadSignature("no signature")
    parts = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
    try:
        return int(parts["t"]), parts["v1"]
    except (KeyError, ValueError):
        raise BadSignature("a malformed signature") from None


class WebhookInbox:
    def __init__(
        self,
        secret: str,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        keep: int = KEEP,
    ) -> None:
        self._secret = secret
        self._now = now
        self._keep = keep
        self._events: OrderedDict[str, ReceivedEvent] = OrderedDict()

    def receive(self, raw: bytes, headers: Mapping[str, str]) -> ReceivedEvent:
        """Keep a delivery whose signature verifies, or raise BadSignature."""
        timestamp, _ = _signed_at(_header(headers, SIGNATURE_HEADER))
        expected = signature(self._secret, timestamp, raw)
        if not hmac.compare_digest(expected, _header(headers, SIGNATURE_HEADER) or ""):
            raise BadSignature("the signature doesn't match")
        signed_at = datetime.fromtimestamp(timestamp, UTC)
        if abs(self._now() - signed_at) > TOLERANCE:
            raise BadSignature("the signature is too old")
        try:
            envelope = json.loads(raw)
            event = ReceivedEvent(
                event_id=str(envelope["id"]),
                event_type=str(envelope["type"]),
                created_at=str(envelope["created_at"]),
                data=dict(envelope["data"]),
                signed_at=signed_at,
            )
        except (ValueError, KeyError, TypeError):
            raise BadSignature("not a Tenant webhook event") from None
        if event.event_type not in TENANT_EVENT_TYPES:
            raise BadSignature("not a Tenant webhook event")
        kept = self._events.setdefault(event.event_id, event)
        while len(self._events) > self._keep:
            self._events.popitem(last=False)
        return kept

    def for_ids(self, ids: Collection[str]) -> list[ReceivedEvent]:
        """The events about any of these ids, in the order they first arrived."""
        wanted = set(ids)
        return [
            event
            for event in self._events.values()
            if any(isinstance(v, str) and v in wanted for v in event.data.values())
        ]
