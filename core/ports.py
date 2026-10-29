"""The seams between domain code and the outside world (CLAUDE.md non-negotiable 2).

Domain code depends only on these Protocols. Each one gets a local
implementation for the MVP and a production implementation later, both in
adapters/. See the seam table in docs/BUILD-PLAN.md.

These are v0 shapes, deliberately small. Phase 1 onwards refines them through
the PRD issues; change a port only alongside the PRD that needs it.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from core.domain.money import Money

# --- Payment rails -----------------------------------------------------------

RailStatus = Literal["succeeded", "pending", "failed"]


class RailPaymentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    idempotency_key: str
    payment_token: str  # a rail token, never a card number or UPI PIN (non-negotiable 7)
    amount: Money
    merchant_ref: str


class RailPaymentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rail_payment_id: str
    status: RailStatus
    failure_code: str | None = None


class PaymentRail(Protocol):
    """Moves money. Only reached after an `allow` decision."""

    name: str

    def charge(self, request: RailPaymentRequest) -> RailPaymentResult: ...


# --- Model gateway -----------------------------------------------------------


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    purpose: str  # e.g. "intent_match", "dispute_narrative"
    model: str
    prompt_version: str
    system: str
    user: str
    max_tokens: int
    timeout_ms: int


class LLMResponse(BaseModel):
    """Usage fields are required so every call is costed (non-negotiable 10)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    model: str
    prompt_version: str
    tokens_in: int
    tokens_out: int
    cost_usd_micros: int
    latency_ms: int


class ModelGateway(Protocol):
    """The only route to an LLM. Never called from core/policy (non-negotiable 1)."""

    def complete(self, request: LLMRequest) -> LLMResponse: ...


# --- Events ------------------------------------------------------------------


class DomainEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    event_type: str
    event_id: str
    occurred_at: datetime
    payload: Mapping[str, object]


class EventBus(Protocol):
    """MVP: a Postgres outbox table. Production: a managed queue, same contract."""

    def publish(self, event: DomainEvent) -> None: ...


# --- Telemetry ---------------------------------------------------------------


class Telemetry(Protocol):
    """Traces and metrics without importing a vendor SDK into core/."""

    def record_span(
        self,
        name: str,
        attributes: Mapping[str, str | int | float | bool],
        duration_ms: int,
    ) -> None: ...


# --- Time ----------------------------------------------------------------------


class Clock(Protocol):
    """Injected so a decision replays byte-identical from its audit record (non-negotiable 5)."""

    def now(self) -> datetime: ...


# --- Persistence ---------------------------------------------------------------


class Store(Protocol):
    """Persistence entry point. Repository methods are added per PRD, each scoped by tenant_id
    (non-negotiable 3)."""

    def ping(self) -> bool: ...
