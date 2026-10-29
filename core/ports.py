"""The seams between domain code and the outside world (CLAUDE.md non-negotiable 2).

Domain code depends only on these Protocols. Each gets an MVP implementation and a
production implementation later, both in adapters/ (seam table in docs/BUILD-PLAN.md).
Signatures carry the non-negotiables in their types:

- money is `Money` (integer minor units + ISO currency), never a float (8);
- every write-shaped method takes an `IdempotencyKey` (4);
- every tenant-scoped method takes a `TenantId` (3);
- every LLM completion returns its usage and cost (10).

Clock and UnitOfWork were added in Phase 1; UnitOfWork replaced the Phase 0 generic
Store. Later PRDs may refine any of them.
"""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from core.domain.audit import SealedAuditRecord, StoredAuditRow
from core.domain.consent import ConsentRequest
from core.domain.decisions import AuditRecordId, Decision, DecisionId
from core.domain.idempotency import IdempotencyKey as IdempotencyKey  # re-exported
from core.domain.idempotency import IdempotencyRecord
from core.domain.intents import IntentCheck, IntentId
from core.domain.mandates import Mandate, MandateId, RevokedBy
from core.domain.money import Money
from core.domain.parties import Agent, AgentId, AgentStatus, Payer, PayerId
from core.domain.parties import TenantId as TenantId  # re-exported for adapters

AttributeValue = str | int | float | bool
Attributes = Mapping[str, AttributeValue]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- Clock ---------------------------------------------------------------------


class Clock(Protocol):
    """The only source of the current time. Read once per request at the API edge and
    passed into the policy engine as a value, never read inside it (ADR-0003)."""

    def now(self) -> datetime:
        """The current instant as an aware UTC datetime."""
        ...


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
    """A model reply plus the usage that non-negotiable 10 requires on every call.
    `stop_reason` is passed through (e.g. "end_turn", "max_tokens", "refusal"), so a
    caller can treat anything but a clean finish as unusable."""

    text: str
    stop_reason: str
    model: str
    prompt_version: str
    tokens_in: int
    tokens_out: int
    cost_usd_micros: int
    latency_ms: int


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ModelGatewayError(Exception):
    """A model call produced no reply. Callers fail closed (non-negotiable 1)."""


class ModelTimeout(ModelGatewayError):
    """No reply within `timeout_ms`."""


class ModelUnavailable(ModelGatewayError):
    """The provider refused or failed the call: a network, auth, rate-limit or server
    error."""


class ModelGateway(Protocol):
    """The only route to an LLM. Never called from core/policy (non-negotiable 1).
    Returns a `Completion`, or raises `ModelTimeout` or `ModelUnavailable`. `effort`
    is passed to the model when given; None leaves the model's default."""

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
        effort: Effort | None = None,
    ) -> Completion: ...


# --- UnitOfWork ----------------------------------------------------------------


class TenantTransaction(Protocol):
    """One atomic piece of work for one Tenant. Every read and write made through it is
    scoped by its tenant_id (non-negotiable 3). Typed operations, such as locking a
    Mandate or appending an Audit record, are added here as features need them."""

    @property
    def tenant_id(self) -> TenantId: ...

    def add_payer(self, payer: Payer) -> None: ...

    def get_payer(self, payer_id: PayerId) -> Payer | None: ...

    def add_agent(self, agent: Agent) -> None: ...

    def get_agent(self, agent_id: AgentId) -> Agent | None: ...

    def set_agent_status(self, agent_id: AgentId, status: AgentStatus) -> Agent | None:
        """Change an Agent's status; None if the Agent isn't in this Tenant."""
        ...

    def add_mandate(self, mandate: Mandate) -> None: ...

    def get_mandate(self, mandate_id: MandateId) -> Mandate | None: ...

    def revoke_mandate(self, mandate_id: MandateId, revoked_by: RevokedBy, at: datetime) -> bool:
        """Revoke an unrevoked Mandate. False if it was already revoked or doesn't exist,
        so two racing revocations can't both succeed."""
        ...

    def list_mandates(
        self, payer_id: PayerId, limit: int, before: tuple[datetime, MandateId] | None
    ) -> list[Mandate]:
        """A Payer's Mandates, newest first by (created_at, mandate_id), strictly older
        than `before` when given."""
        ...

    def lock_mandate(self, mandate_id: MandateId) -> Mandate | None:
        """Read a Mandate and hold it until this transaction ends, so Decisions on one
        Mandate run one after another. Always taken before the chain head."""
        ...

    def add_decision(self, decision: Decision) -> None: ...

    def allowed_spend(self, mandate_id: MandateId, start: datetime, end: datetime) -> int:
        """Total minor units of allowed Decisions on a Mandate decided in [start, end)."""
        ...

    def count_decisions(self, mandate_id: MandateId, after: datetime, until: datetime) -> int:
        """Decisions on a Mandate, of any Outcome, decided in (after, until]: Velocity.
        `until` is inclusive so earlier Decisions at the same instant count."""
        ...

    def add_consent_request(self, consent: ConsentRequest) -> None: ...

    def add_intent_check(self, check: IntentCheck) -> None: ...

    def get_intent_check(self, intent_id: IntentId) -> IntentCheck | None: ...

    def get_consent_request(self, decision_id: DecisionId) -> ConsentRequest | None: ...

    def get_decision(self, decision_id: DecisionId) -> Decision | None: ...

    def lock_chain_head(self) -> tuple[int, str]:
        """The Tenant's last Audit record (seq, record_hash), held until this transaction
        ends; (0, ZERO_HASH) before the first record."""
        ...

    def append_audit_record(self, sealed: SealedAuditRecord) -> None:
        """Add the next record and move the chain head. Records are never changed."""
        ...

    def get_audit_record(self, audit_record_id: AuditRecordId) -> SealedAuditRecord | None: ...

    def read_audit_chain(self) -> tuple[list[StoredAuditRow], tuple[int, str]]:
        """Every stored Audit record in seq order, and the chain head, without locking."""
        ...

    def claim_idempotency_key(self, key: IdempotencyKey) -> bool:
        """Hold `key` until this transaction ends. False, without waiting, if another
        transaction holds it: that call is still in flight."""
        ...

    def get_idempotency_record(self, key: IdempotencyKey) -> IdempotencyRecord | None: ...

    def save_idempotency_record(self, record: IdempotencyRecord) -> None:
        """Store a response, replacing an expired record with the same key."""
        ...


class UnitOfWork(Protocol):
    """Opens tenant-scoped transactions. Work done in one commits together when the block
    exits cleanly and is discarded if it raises, so a Decision, its Audit record and its
    idempotency entry are stored together or not at all (ADR-0004)."""

    def transaction(self, tenant_id: TenantId) -> AbstractContextManager[TenantTransaction]: ...


class TenantDirectory(Protocol):
    """Tenants and their API keys. The only lookup that isn't tenant-scoped, because it
    is how a request's Tenant is found. Keys are stored as hashes only."""

    def create_tenant(
        self, tenant_id: TenantId, name: str, key_prefix: str, key_hash: str, created_at: datetime
    ) -> None: ...

    def tenant_for_key(self, key_hash: str) -> TenantId | None: ...


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
