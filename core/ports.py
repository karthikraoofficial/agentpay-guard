"""The five seams between domain code and the outside world (CLAUDE.md non-negotiable 2).

Domain code depends only on these Protocols. Each gets an MVP implementation and a
production implementation later, both in adapters/ (seam table in docs/BUILD-PLAN.md).
Signatures carry the non-negotiables in their types:

- money is `Money` (integer minor units + ISO currency), never a float (8);
- every write-shaped method takes an `IdempotencyKey` (4);
- every tenant-scoped method takes a `TenantId` (3);
- every LLM completion returns its usage and cost (10).

No implementation depends on these yet, so later PRDs may refine them.
"""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Literal, NewType, Protocol

from pydantic import BaseModel, ConfigDict

from core.domain.money import Money

TenantId = NewType("TenantId", str)
IdempotencyKey = NewType("IdempotencyKey", str)

AttributeValue = str | int | float | bool
Attributes = Mapping[str, AttributeValue]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- PaymentRail -------------------------------------------------------------

RailStatus = Literal["authorised", "captured", "refunded", "pending", "failed"]


class RailResult(_Frozen):
    rail_payment_id: str
    status: RailStatus
    amount: Money
    failure_code: str | None = None


class PaymentRail(Protocol):
    """Moves money. Only reached after an `allow` decision. `payment_token` is a rail token,
    never a card number or UPI PIN (non-negotiable 7)."""

    def authorise(
        self,
        tenant_id: TenantId,
        payment_token: str,
        amount: Money,
        merchant_ref: str,
        idempotency_key: IdempotencyKey,
    ) -> RailResult: ...

    def capture(
        self,
        tenant_id: TenantId,
        rail_payment_id: str,
        amount: Money,
        idempotency_key: IdempotencyKey,
    ) -> RailResult: ...

    def refund(
        self,
        tenant_id: TenantId,
        rail_payment_id: str,
        amount: Money,
        idempotency_key: IdempotencyKey,
    ) -> RailResult: ...


# --- ModelGateway ------------------------------------------------------------


class Completion(_Frozen):
    """A model reply plus the usage that non-negotiable 10 requires on every call."""

    text: str
    model: str
    prompt_version: str
    tokens_in: int
    tokens_out: int
    cost_usd_micros: int
    latency_ms: int


class ModelGateway(Protocol):
    """The only route to an LLM. Never called from core/policy (non-negotiable 1)."""

    def complete(
        self,
        tenant_id: TenantId,
        purpose: str,
        model: str,
        prompt_version: str,
        system: str,
        user: str,
        max_tokens: int,
        timeout_ms: int,
    ) -> Completion: ...


# --- Store ---------------------------------------------------------------------


class Store(Protocol):
    """Tenant-scoped persistence. Every read and write is scoped by tenant_id."""

    def get(
        self, tenant_id: TenantId, collection: str, key: str
    ) -> Mapping[str, object] | None: ...

    def put(
        self,
        tenant_id: TenantId,
        collection: str,
        key: str,
        value: Mapping[str, object],
        idempotency_key: IdempotencyKey,
    ) -> None: ...


# --- EventBus ------------------------------------------------------------------


class DomainEvent(_Frozen):
    tenant_id: TenantId
    event_type: str
    occurred_at: datetime
    payload: Mapping[str, object]


class EventBus(Protocol):
    """MVP: a Postgres outbox table. Production: a managed queue with the same contract.
    Publishing twice with the same key delivers the event once."""

    def publish(self, event: DomainEvent, idempotency_key: IdempotencyKey) -> None: ...


# --- Telemetry -----------------------------------------------------------------


class Span(Protocol):
    def set_attribute(self, key: str, value: AttributeValue) -> None: ...


class Telemetry(Protocol):
    """Traces and metrics without importing a vendor SDK into core/."""

    def start_span(self, name: str, attributes: Attributes) -> AbstractContextManager[Span]: ...

    def record_metric(self, name: str, value: float, attributes: Attributes) -> None: ...
