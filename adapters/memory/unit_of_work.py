import threading
from collections.abc import Iterator
from collections.abc import Set as AbstractSet
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from core.domain.audit import ZERO_HASH, SealedAuditRecord, StoredAuditRow
from core.domain.consent import (
    ConsentLink,
    ConsentRequest,
    ConsentRequestId,
    ConsentResolution,
    ConsentStatus,
)
from core.domain.decisions import AuditRecordId, Decision, DecisionId
from core.domain.idempotency import IdempotencyKey, IdempotencyRecord
from core.domain.intents import IntentCheck, IntentId
from core.domain.mandates import Mandate, MandateId, RevokedBy
from core.domain.parties import (
    Agent,
    AgentId,
    AgentStatus,
    ApiKeyGrant,
    ApiKeyScope,
    Payer,
    PayerId,
)
from core.domain.payments import Payment, PaymentId, RailEvent
from core.domain.voids import Void
from core.domain.webhooks import WebhookEndpoint
from core.ports import DomainEvent, EventBus, TenantDirectory, TenantId, UnitOfWork


@dataclass
class _TenantData:
    payers: dict[PayerId, Payer] = field(default_factory=dict)
    agents: dict[AgentId, Agent] = field(default_factory=dict)
    idempotency: dict[IdempotencyKey, IdempotencyRecord] = field(default_factory=dict)
    mandates: dict[MandateId, Mandate] = field(default_factory=dict)
    decisions: dict[DecisionId, Decision] = field(default_factory=dict)
    audit: dict[AuditRecordId, SealedAuditRecord] = field(default_factory=dict)
    head: tuple[int, str] = (0, ZERO_HASH)
    consents: dict[DecisionId, ConsentRequest] = field(default_factory=dict)
    intents: dict[IntentId, IntentCheck] = field(default_factory=dict)
    # event_id -> (event, done: delivered or given up, not before: when a retry is due)
    outbox: dict[str, tuple[DomainEvent, bool, datetime | None]] = field(default_factory=dict)
    links: dict[str, ConsentLink] = field(default_factory=dict)
    payments: dict[PaymentId, Payment] = field(default_factory=dict)
    voids: dict[DecisionId, Void] = field(default_factory=dict)
    rail_events: dict[str, RailEvent] = field(default_factory=dict)
    webhook: WebhookEndpoint | None = None

    def copy(self) -> "_TenantData":
        return _TenantData(
            dict(self.payers),
            dict(self.agents),
            dict(self.idempotency),
            dict(self.mandates),
            dict(self.decisions),
            dict(self.audit),
            self.head,
            dict(self.consents),
            dict(self.intents),
            dict(self.outbox),
            dict(self.links),
            dict(self.payments),
            dict(self.voids),
            dict(self.rail_events),
            self.webhook,
        )


class _Claims:
    """Idempotency keys held by open transactions, shared across threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: set[tuple[TenantId, IdempotencyKey]] = set()

    def try_claim(self, claim: tuple[TenantId, IdempotencyKey]) -> bool:
        with self._lock:
            if claim in self._held:
                return False
            self._held.add(claim)
            return True

    def release(self, claims: set[tuple[TenantId, IdempotencyKey]]) -> None:
        with self._lock:
            self._held -= claims


class InMemoryTransaction:
    def __init__(self, tenant_id: TenantId, data: _TenantData, claims: _Claims) -> None:
        self._tenant_id = tenant_id
        self._data = data
        self._claims = claims
        self.held: set[tuple[TenantId, IdempotencyKey]] = set()

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id

    def add_payer(self, payer: Payer) -> None:
        self._own(payer.tenant_id)
        self._data.payers[payer.payer_id] = payer

    def get_payer(self, payer_id: PayerId) -> Payer | None:
        return self._data.payers.get(payer_id)

    def add_agent(self, agent: Agent) -> None:
        self._own(agent.tenant_id)
        self._data.agents[agent.agent_id] = agent

    def get_agent(self, agent_id: AgentId) -> Agent | None:
        return self._data.agents.get(agent_id)

    def set_agent_status(self, agent_id: AgentId, status: AgentStatus) -> Agent | None:
        agent = self._data.agents.get(agent_id)
        if agent is None:
            return None
        updated = agent.model_copy(update={"status": status})
        self._data.agents[agent_id] = updated
        return updated

    def add_mandate(self, mandate: Mandate) -> None:
        self._own(mandate.tenant_id)
        self._data.mandates[mandate.mandate_id] = mandate

    def get_mandate(self, mandate_id: MandateId) -> Mandate | None:
        return self._data.mandates.get(mandate_id)

    def revoke_mandate(self, mandate_id: MandateId, revoked_by: RevokedBy, at: datetime) -> bool:
        mandate = self._data.mandates.get(mandate_id)
        if mandate is None or mandate.revoked_at is not None:
            return False
        self._data.mandates[mandate_id] = mandate.model_copy(
            update={"revoked_by": revoked_by, "revoked_at": at}
        )
        return True

    def list_mandates(
        self, payer_id: PayerId, limit: int, before: tuple[datetime, MandateId] | None
    ) -> list[Mandate]:
        rows = sorted(
            (m for m in self._data.mandates.values() if m.payer_id == payer_id),
            key=lambda m: (m.created_at, m.mandate_id),
            reverse=True,
        )
        if before is not None:
            rows = [m for m in rows if (m.created_at, m.mandate_id) < before]
        return rows[:limit]

    def lock_mandate(self, mandate_id: MandateId) -> Mandate | None:
        return self._data.mandates.get(mandate_id)

    def add_decision(self, decision: Decision) -> None:
        self._own(decision.tenant_id)
        self._data.decisions[decision.decision_id] = decision

    def get_decision(self, decision_id: DecisionId) -> Decision | None:
        return self._data.decisions.get(decision_id)

    def get_webhook_endpoint(self) -> WebhookEndpoint | None:
        return self._data.webhook

    def save_webhook_endpoint(self, endpoint: WebhookEndpoint) -> None:
        self._own(endpoint.tenant_id)
        self._data.webhook = endpoint

    def allowed_spend(self, mandate_id: MandateId, start: datetime, end: datetime) -> int:
        decisions, voided = self._data.decisions, self._data.voids
        allowed = sum(
            d.amount.minor
            for d in decisions.values()
            if d.mandate_id == mandate_id
            and d.outcome == "allow"
            and start <= d.decided_at < end
            and d.decision_id not in voided
        )
        approved = sum(
            decisions[c.decision_id].amount.minor
            for c in self._data.consents.values()
            if c.resolution is not None
            and c.resolution.status == "approved"
            and c.decision_id not in voided
            and decisions[c.decision_id].mandate_id == mandate_id
            and start <= c.resolution.resolved_at < end
        )
        return allowed + approved

    def has_allowed_payment_to(self, payer_id: PayerId, merchant_id: str) -> bool:
        mandates = self._data.mandates
        return any(
            d.outcome == "allow"
            and d.merchant_id == merchant_id
            and mandates[d.mandate_id].payer_id == payer_id
            and d.decision_id not in self._data.voids
            for d in self._data.decisions.values()
        )

    def count_decisions(self, mandate_id: MandateId, after: datetime, until: datetime) -> int:
        return sum(
            1
            for d in self._data.decisions.values()
            if d.mandate_id == mandate_id and after < d.decided_at <= until
        )

    def add_payment(self, payment: Payment) -> None:
        self._own(payment.tenant_id)
        if self.get_payment_for_decision(payment.decision_id) is not None:
            raise ValueError(f"Decision {payment.decision_id} already has a Payment")
        self._data.payments[payment.payment_id] = payment

    def get_payment(self, payment_id: PaymentId) -> Payment | None:
        return self._data.payments.get(payment_id)

    def get_payment_for_decision(self, decision_id: DecisionId) -> Payment | None:
        return next((p for p in self._data.payments.values() if p.decision_id == decision_id), None)

    def lock_payment(self, payment_id: PaymentId) -> Payment | None:
        return self._data.payments.get(payment_id)

    def add_rail_event(self, event: RailEvent) -> None:
        self._own(event.tenant_id)
        if event.event_id in self._data.rail_events:
            raise ValueError(f"rail event {event.event_id} is already kept")
        self._data.rail_events[event.event_id] = event

    def get_rail_event(self, event_id: str) -> RailEvent | None:
        return self._data.rail_events.get(event_id)

    def add_void(self, void: Void) -> None:
        self._own(void.tenant_id)
        if void.decision_id in self._data.voids:
            raise ValueError(f"Decision {void.decision_id} is already Voided")
        self._data.voids[void.decision_id] = void

    def get_void(self, decision_id: DecisionId) -> Void | None:
        return self._data.voids.get(decision_id)

    def update_payment(self, payment: Payment) -> None:
        self._own(payment.tenant_id)
        if payment.payment_id not in self._data.payments:
            raise ValueError(f"no Payment {payment.payment_id}")
        self._data.payments[payment.payment_id] = payment

    def add_intent_check(self, check: IntentCheck) -> None:
        self._own(check.tenant_id)
        self._data.intents[check.intent_id] = check

    def get_intent_check(self, intent_id: IntentId) -> IntentCheck | None:
        return self._data.intents.get(intent_id)

    def add_consent_request(self, consent: ConsentRequest) -> None:
        self._own(consent.tenant_id)
        self._data.consents[consent.decision_id] = consent

    def get_consent_request(self, decision_id: DecisionId) -> ConsentRequest | None:
        return self._data.consents.get(decision_id)

    def find_consent_request(self, consent_request_id: ConsentRequestId) -> ConsentRequest | None:
        return next(
            (c for c in self._data.consents.values() if c.consent_request_id == consent_request_id),
            None,
        )

    def list_consent_requests(
        self,
        status: ConsentStatus | None,
        now: datetime,
        limit: int,
        before: tuple[datetime, ConsentRequestId] | None,
    ) -> list[ConsentRequest]:
        rows = sorted(
            (c for c in self._data.consents.values() if status in (None, c.status(now))),
            key=lambda c: (c.created_at, c.consent_request_id),
            reverse=True,
        )
        if before is not None:
            rows = [c for c in rows if (c.created_at, c.consent_request_id) < before]
        return rows[:limit]

    def lock_consent_request(self, consent_request_id: ConsentRequestId) -> ConsentRequest | None:
        return self.find_consent_request(consent_request_id)

    def add_consent_link(self, link: ConsentLink) -> None:
        self._own(link.tenant_id)
        links = self._data.links
        for token_hash, old in list(links.items()):
            if old.consent_request_id == link.consent_request_id and old.revoked_at is None:
                links[token_hash] = old.model_copy(update={"revoked_at": link.created_at})
        links[link.token_hash] = link

    def get_consent_link(self, token_hash: str) -> ConsentLink | None:
        return self._data.links.get(token_hash)

    def publish(self, event: DomainEvent) -> None:
        self._own(event.tenant_id)
        self._data.outbox.setdefault(event.event_id, (event, False, None))

    def add_consent_resolution(self, resolution: ConsentResolution) -> None:
        self._own(resolution.tenant_id)
        consent = self._data.consents.get(resolution.decision_id)
        if consent is None or consent.consent_request_id != resolution.consent_request_id:
            raise ValueError(f"no Consent request {resolution.consent_request_id}")
        if consent.resolution is not None:
            raise ValueError(f"Consent request {resolution.consent_request_id} is resolved")
        self._data.consents[resolution.decision_id] = consent.model_copy(
            update={"resolution": resolution}
        )

    def lock_chain_head(self) -> tuple[int, str]:
        return self._data.head

    def append_audit_record(self, sealed: SealedAuditRecord) -> None:
        self._own(sealed.record.tenant_id)
        if (sealed.seq - 1, sealed.prev_hash) != self._data.head:
            raise ValueError("Audit record does not follow the chain head")
        self._data.audit[sealed.record.audit_record_id] = sealed
        self._data.head = (sealed.seq, sealed.record_hash)

    def get_audit_record(self, audit_record_id: AuditRecordId) -> SealedAuditRecord | None:
        return self._data.audit.get(audit_record_id)

    def read_audit_chain(self) -> tuple[list[StoredAuditRow], tuple[int, str]]:
        rows = sorted(
            (StoredAuditRow.from_sealed(s) for s in self._data.audit.values()),
            key=lambda r: r.seq,
        )
        return rows, self._data.head

    def claim_idempotency_key(self, key: IdempotencyKey) -> bool:
        claim = (self._tenant_id, key)
        if claim in self.held:
            return True
        if not self._claims.try_claim(claim):
            return False
        self.held.add(claim)
        return True

    def get_idempotency_record(self, key: IdempotencyKey) -> IdempotencyRecord | None:
        return self._data.idempotency.get(key)

    def save_idempotency_record(self, record: IdempotencyRecord) -> None:
        self._data.idempotency[record.key] = record

    def _own(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tenant_id:
            raise ValueError(f"record for {tenant_id} written in a transaction for another Tenant")


class InMemoryUnitOfWork:
    """The store held in process memory, for tests that don't need Postgres. A
    transaction works on a copy of its Tenant's data, kept only if the block succeeds."""

    def __init__(self) -> None:
        self._tenants: dict[TenantId, _TenantData] = {}
        self._keys: dict[str, ApiKeyGrant] = {}
        self._claims = _Claims()
        self._commit = threading.Lock()

    @contextmanager
    def transaction(self, tenant_id: TenantId) -> Iterator[InMemoryTransaction]:
        # Copy-on-transaction: concurrent transactions for one Tenant would overwrite
        # each other, so tests that need real concurrency use the Postgres store.
        working = self._tenants.get(tenant_id, _TenantData()).copy()
        tx = InMemoryTransaction(tenant_id, working, self._claims)
        try:
            yield tx
            with self._commit:
                self._tenants[tenant_id] = working
        finally:
            self._claims.release(tx.held)

    def create_tenant(self, tenant_id: TenantId, name: str, created_at: datetime) -> None:
        self._tenants.setdefault(tenant_id, _TenantData())

    def add_api_key(
        self,
        tenant_id: TenantId,
        key_prefix: str,
        key_hash: str,
        scopes: AbstractSet[ApiKeyScope],
        created_at: datetime,
    ) -> bool:
        if tenant_id not in self._tenants:
            return False
        self._keys[key_hash] = ApiKeyGrant(tenant_id=tenant_id, scopes=frozenset(scopes))
        return True

    def api_key(self, key_hash: str) -> ApiKeyGrant | None:
        return self._keys.get(key_hash)

    def consent_link(self, token_hash: str) -> ConsentLink | None:
        for data in list(self._tenants.values()):
            if token_hash in data.links:
                return data.links[token_hash]
        return None

    def scheduled_payments(
        self, created_before: datetime, now: datetime
    ) -> list[tuple[TenantId, PaymentId]]:
        due = [
            (p.created_at, tenant_id, p.payment_id)
            for tenant_id, data in list(self._tenants.items())
            for p in data.payments.values()
            if p.status == "scheduled"
            and p.created_at < created_before
            and (p.debit_after is None or p.debit_after <= now)
        ]
        return [(tenant_id, payment_id) for _, tenant_id, payment_id in sorted(due)]

    def due_debits(self, now: datetime) -> list[tuple[TenantId, PaymentId]]:
        due = [
            (p.debit_after, tenant_id, p.payment_id)
            for tenant_id, data in list(self._tenants.items())
            for p in data.payments.values()
            if p.debit_due(now)
        ]
        return [(tenant_id, payment_id) for _, tenant_id, payment_id in sorted(due)]

    def payment_tenant(self, payment_id: PaymentId) -> TenantId | None:
        for tenant_id, data in list(self._tenants.items()):
            if payment_id in data.payments:
                return tenant_id
        return None

    def undelivered(self, limit: int, now: datetime | None = None) -> list[DomainEvent]:
        waiting = [
            event
            for data in list(self._tenants.values())
            for event, done, due in list(data.outbox.values())
            if not done and (now is None or due is None or due <= now)
        ]
        return sorted(waiting, key=lambda e: (e.occurred_at, e.event_id))[:limit]

    def mark_delivered(self, tenant_id: TenantId, event_id: str, at: datetime) -> None:
        self._outboxed(tenant_id, event_id, done=True)

    def retry_later(self, tenant_id: TenantId, event_id: str, at: datetime) -> None:
        self._outboxed(tenant_id, event_id, done=False, due=at, failed=True)

    def give_up(self, tenant_id: TenantId, event_id: str, at: datetime) -> None:
        self._outboxed(tenant_id, event_id, done=True)

    def _outboxed(
        self,
        tenant_id: TenantId,
        event_id: str,
        *,
        done: bool,
        due: datetime | None = None,
        failed: bool = False,
    ) -> None:
        with self._commit:
            data = self._tenants.get(tenant_id)
            if data is None or event_id not in data.outbox:
                return
            event = data.outbox[event_id][0]
            if failed:
                event = event.model_copy(update={"attempts": event.attempts + 1})
            data.outbox[event_id] = (event, done, due)


# mypy checks the fake matches the ports
_conforms_uow: type[UnitOfWork] = InMemoryUnitOfWork
_conforms_directory: type[TenantDirectory] = InMemoryUnitOfWork
_conforms_bus: type[EventBus] = InMemoryUnitOfWork
