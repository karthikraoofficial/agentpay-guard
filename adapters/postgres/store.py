from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import Connection, Engine, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from adapters.postgres.tables import agents, api_keys, idempotency_keys, payers, tenants
from core.domain.idempotency import IdempotencyKey, IdempotencyRecord
from core.domain.parties import Agent, AgentId, AgentStatus, Payer, PayerId
from core.ports import TenantDirectory, TenantId, UnitOfWork


class PostgresTransaction:
    """One database transaction. Every statement filters by this Tenant's id."""

    def __init__(self, conn: Connection, tenant_id: TenantId) -> None:
        self._conn = conn
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id

    def add_payer(self, payer: Payer) -> None:
        self._own(payer.tenant_id)
        self._conn.execute(insert(payers).values(**payer.model_dump()))

    def get_payer(self, payer_id: PayerId) -> Payer | None:
        row = self._conn.execute(
            select(payers).where(
                payers.c.tenant_id == self._tenant_id, payers.c.payer_id == payer_id
            )
        ).one_or_none()
        return None if row is None else Payer.model_validate(row._asdict())

    def add_agent(self, agent: Agent) -> None:
        self._own(agent.tenant_id)
        self._conn.execute(insert(agents).values(**agent.model_dump()))

    def get_agent(self, agent_id: AgentId) -> Agent | None:
        row = self._conn.execute(
            select(agents).where(
                agents.c.tenant_id == self._tenant_id, agents.c.agent_id == agent_id
            )
        ).one_or_none()
        return None if row is None else Agent.model_validate(row._asdict())

    def set_agent_status(self, agent_id: AgentId, status: AgentStatus) -> Agent | None:
        row = self._conn.execute(
            update(agents)
            .where(agents.c.tenant_id == self._tenant_id, agents.c.agent_id == agent_id)
            .values(status=status)
            .returning(*agents.c)
        ).one_or_none()
        return None if row is None else Agent.model_validate(row._asdict())

    def claim_idempotency_key(self, key: IdempotencyKey) -> bool:
        # A transaction-scoped advisory lock: released at commit or rollback, and a second
        # caller gets False at once instead of queueing behind the first. Tenant ids never
        # contain ':', so the first ':' separates tenant from key.
        lock_id = func.hashtextextended(f"{self._tenant_id}:{key}", 0)
        held: bool = self._conn.execute(
            select(func.pg_try_advisory_xact_lock(lock_id))
        ).scalar_one()
        return held

    def get_idempotency_record(self, key: IdempotencyKey) -> IdempotencyRecord | None:
        row = self._conn.execute(
            select(idempotency_keys).where(
                idempotency_keys.c.tenant_id == self._tenant_id, idempotency_keys.c.key == key
            )
        ).one_or_none()
        if row is None:
            return None
        return IdempotencyRecord.model_validate(
            {k: v for k, v in row._asdict().items() if k != "tenant_id"}
        )

    def save_idempotency_record(self, record: IdempotencyRecord) -> None:
        values = {"tenant_id": self._tenant_id, **record.model_dump()}
        statement = pg_insert(idempotency_keys).values(**values)
        self._conn.execute(
            statement.on_conflict_do_update(
                index_elements=["tenant_id", "key"],
                set_={c: statement.excluded[c] for c in values if c not in ("tenant_id", "key")},
            )
        )

    def _own(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tenant_id:
            raise ValueError(f"record for {tenant_id} written in a transaction for another Tenant")


class PostgresStore:
    """The UnitOfWork and TenantDirectory backed by Postgres."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def transaction(self, tenant_id: TenantId) -> Iterator[PostgresTransaction]:
        with self._engine.begin() as conn:
            yield PostgresTransaction(conn, tenant_id)

    def create_tenant(
        self, tenant_id: TenantId, name: str, key_prefix: str, key_hash: str, created_at: datetime
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                insert(tenants).values(tenant_id=tenant_id, name=name, created_at=created_at)
            )
            conn.execute(
                insert(api_keys).values(
                    key_hash=key_hash,
                    tenant_id=tenant_id,
                    key_prefix=key_prefix,
                    created_at=created_at,
                )
            )

    def tenant_for_key(self, key_hash: str) -> TenantId | None:
        with self._engine.connect() as conn:
            tenant_id = conn.execute(
                select(api_keys.c.tenant_id).where(api_keys.c.key_hash == key_hash)
            ).scalar_one_or_none()
        return None if tenant_id is None else TenantId(tenant_id)


_conforms_uow: type[UnitOfWork] = PostgresStore  # mypy checks the adapter matches the ports
_conforms_directory: type[TenantDirectory] = PostgresStore
