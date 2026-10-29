import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from core.domain.idempotency import IdempotencyKey, IdempotencyRecord
from core.domain.mandates import Mandate, MandateId, RevokedBy
from core.domain.parties import Agent, AgentId, AgentStatus, Payer, PayerId
from core.ports import TenantDirectory, TenantId, UnitOfWork


@dataclass
class _TenantData:
    payers: dict[PayerId, Payer] = field(default_factory=dict)
    agents: dict[AgentId, Agent] = field(default_factory=dict)
    idempotency: dict[IdempotencyKey, IdempotencyRecord] = field(default_factory=dict)
    mandates: dict[MandateId, Mandate] = field(default_factory=dict)

    def copy(self) -> "_TenantData":
        return _TenantData(
            dict(self.payers), dict(self.agents), dict(self.idempotency), dict(self.mandates)
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
        self._keys: dict[str, TenantId] = {}
        self._claims = _Claims()
        self._commit = threading.Lock()

    @contextmanager
    def transaction(self, tenant_id: TenantId) -> Iterator[InMemoryTransaction]:
        working = self._tenants.get(tenant_id, _TenantData()).copy()
        tx = InMemoryTransaction(tenant_id, working, self._claims)
        try:
            yield tx
            with self._commit:
                self._tenants[tenant_id] = working
        finally:
            self._claims.release(tx.held)

    def create_tenant(
        self, tenant_id: TenantId, name: str, key_prefix: str, key_hash: str, created_at: datetime
    ) -> None:
        self._tenants.setdefault(tenant_id, _TenantData())
        self._keys[key_hash] = tenant_id

    def tenant_for_key(self, key_hash: str) -> TenantId | None:
        return self._keys.get(key_hash)


# mypy checks the fake matches the ports
_conforms_uow: type[UnitOfWork] = InMemoryUnitOfWork
_conforms_directory: type[TenantDirectory] = InMemoryUnitOfWork
