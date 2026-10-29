from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import Connection, Engine, RowMapping, func, insert, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from adapters.postgres.tables import (
    agents,
    api_keys,
    audit_records,
    consent_requests,
    decisions,
    idempotency_keys,
    intent_checks,
    mandates,
    payers,
    tenant_chain_heads,
    tenants,
)
from core.domain.audit import ZERO_HASH, SealedAuditRecord, StoredAuditRow, unseal
from core.domain.consent import ConsentRequest
from core.domain.decisions import AuditRecordId, Decision, DecisionId, PackRef
from core.domain.idempotency import IdempotencyKey, IdempotencyRecord
from core.domain.intents import IntentCheck, IntentContent, IntentId
from core.domain.mandates import ConsentEvidence, Mandate, MandateId, RevokedBy
from core.domain.money import Money
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

    def add_mandate(self, mandate: Mandate) -> None:
        self._own(mandate.tenant_id)
        self._conn.execute(insert(mandates).values(**_mandate_row(mandate)))

    def get_mandate(self, mandate_id: MandateId) -> Mandate | None:
        row = (
            self._conn.execute(
                select(mandates).where(
                    mandates.c.tenant_id == self._tenant_id, mandates.c.mandate_id == mandate_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _mandate(row)

    def revoke_mandate(self, mandate_id: MandateId, revoked_by: RevokedBy, at: datetime) -> bool:
        result = self._conn.execute(
            update(mandates)
            .where(
                mandates.c.tenant_id == self._tenant_id,
                mandates.c.mandate_id == mandate_id,
                mandates.c.revoked_at.is_(None),
            )
            .values(revoked_by=revoked_by, revoked_at=at)
        )
        return result.rowcount == 1

    def list_mandates(
        self, payer_id: PayerId, limit: int, before: tuple[datetime, MandateId] | None
    ) -> list[Mandate]:
        query = select(mandates).where(
            mandates.c.tenant_id == self._tenant_id, mandates.c.payer_id == payer_id
        )
        if before is not None:
            query = query.where(tuple_(mandates.c.created_at, mandates.c.mandate_id) < before)
        query = query.order_by(mandates.c.created_at.desc(), mandates.c.mandate_id.desc())
        return [_mandate(r) for r in self._conn.execute(query.limit(limit)).mappings()]

    def lock_mandate(self, mandate_id: MandateId) -> Mandate | None:
        row = (
            self._conn.execute(
                select(mandates)
                .where(mandates.c.tenant_id == self._tenant_id, mandates.c.mandate_id == mandate_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _mandate(row)

    def add_decision(self, decision: Decision) -> None:
        self._own(decision.tenant_id)
        self._conn.execute(
            insert(decisions).values(
                tenant_id=decision.tenant_id,
                decision_id=decision.decision_id,
                mandate_id=decision.mandate_id,
                agent_id=decision.agent_id,
                amount_minor=decision.amount.minor,
                currency=decision.amount.currency,
                outcome=decision.outcome,
                reason_codes=list(decision.reason_codes),
                risk_tier=decision.risk_tier,
                pack_id=decision.policy_pack.id,
                pack_version=decision.policy_pack.version,
                pack_hash=decision.policy_pack.hash,
                audit_record_id=decision.audit_record_id,
                decided_at=decision.decided_at,
            )
        )

    def allowed_spend(self, mandate_id: MandateId, start: datetime, end: datetime) -> int:
        total = self._conn.execute(
            select(func.coalesce(func.sum(decisions.c.amount_minor), 0)).where(
                decisions.c.tenant_id == self._tenant_id,
                decisions.c.mandate_id == mandate_id,
                decisions.c.outcome == "allow",
                decisions.c.decided_at >= start,
                decisions.c.decided_at < end,
            )
        ).scalar_one()
        return int(total)

    def count_decisions(self, mandate_id: MandateId, after: datetime, until: datetime) -> int:
        total = self._conn.execute(
            select(func.count()).where(
                decisions.c.tenant_id == self._tenant_id,
                decisions.c.mandate_id == mandate_id,
                decisions.c.decided_at > after,
                decisions.c.decided_at <= until,
            )
        ).scalar_one()
        return int(total)

    def add_consent_request(self, consent: ConsentRequest) -> None:
        self._own(consent.tenant_id)
        self._conn.execute(insert(consent_requests).values(**consent.model_dump()))

    def get_consent_request(self, decision_id: DecisionId) -> ConsentRequest | None:
        row = (
            self._conn.execute(
                select(consent_requests).where(
                    consent_requests.c.tenant_id == self._tenant_id,
                    consent_requests.c.decision_id == decision_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else ConsentRequest.model_validate(dict(row))

    def add_intent_check(self, check: IntentCheck) -> None:
        self._own(check.tenant_id)
        c = check.content
        self._conn.execute(
            insert(intent_checks).values(
                **check.model_dump(exclude={"content"}),
                merchant_id=c.merchant.id,
                merchant_mcc=c.merchant.mcc,
                quoted_total_minor=c.quoted_total.minor,
                currency=c.quoted_total.currency,
                content=c.canonical(),
            )
        )

    def get_intent_check(self, intent_id: IntentId) -> IntentCheck | None:
        row = (
            self._conn.execute(
                select(intent_checks).where(
                    intent_checks.c.tenant_id == self._tenant_id,
                    intent_checks.c.intent_id == intent_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        fields = {k: v for k, v in row.items() if k in IntentCheck.model_fields}
        content = IntentContent.model_validate_json(row["content"])
        return IntentCheck.model_validate({**fields, "content": content})

    def get_decision(self, decision_id: DecisionId) -> Decision | None:
        row = (
            self._conn.execute(
                select(decisions).where(
                    decisions.c.tenant_id == self._tenant_id,
                    decisions.c.decision_id == decision_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return Decision(
            decision_id=row["decision_id"],
            tenant_id=row["tenant_id"],
            mandate_id=row["mandate_id"],
            agent_id=row["agent_id"],
            amount=Money(minor=row["amount_minor"], currency=row["currency"]),
            outcome=row["outcome"],
            reason_codes=tuple(row["reason_codes"]),
            risk_tier=row["risk_tier"],
            policy_pack=PackRef(
                id=row["pack_id"], version=row["pack_version"], hash=row["pack_hash"]
            ),
            audit_record_id=row["audit_record_id"],
            decided_at=row["decided_at"],
        )

    def lock_chain_head(self) -> tuple[int, str]:
        self._conn.execute(
            pg_insert(tenant_chain_heads)
            .values(tenant_id=self._tenant_id, seq=0, record_hash=ZERO_HASH)
            .on_conflict_do_nothing(index_elements=["tenant_id"])
        )
        row = self._conn.execute(
            select(tenant_chain_heads.c.seq, tenant_chain_heads.c.record_hash)
            .where(tenant_chain_heads.c.tenant_id == self._tenant_id)
            .with_for_update()
        ).one()
        return int(row.seq), str(row.record_hash)

    def append_audit_record(self, sealed: SealedAuditRecord) -> None:
        record = sealed.record
        self._own(record.tenant_id)
        self._conn.execute(
            insert(audit_records).values(
                tenant_id=record.tenant_id,
                seq=record.seq,
                audit_record_id=record.audit_record_id,
                kind=record.kind,
                decision_id=record.decision_id,
                body=sealed.body,
                prev_hash=record.prev_hash,
                record_hash=sealed.record_hash,
            )
        )
        moved = self._conn.execute(
            update(tenant_chain_heads)
            .where(
                tenant_chain_heads.c.tenant_id == self._tenant_id,
                tenant_chain_heads.c.seq == record.seq - 1,
                tenant_chain_heads.c.record_hash == record.prev_hash,
            )
            .values(seq=record.seq, record_hash=sealed.record_hash)
        )
        if moved.rowcount != 1:
            raise ValueError("Audit record does not follow the chain head")

    def get_audit_record(self, audit_record_id: AuditRecordId) -> SealedAuditRecord | None:
        row = self._conn.execute(
            select(audit_records.c.body, audit_records.c.record_hash).where(
                audit_records.c.tenant_id == self._tenant_id,
                audit_records.c.audit_record_id == audit_record_id,
            )
        ).one_or_none()
        return None if row is None else unseal(bytes(row.body), row.record_hash)

    def read_audit_chain(self) -> tuple[list[StoredAuditRow], tuple[int, str]]:
        rows = self._conn.execute(
            select(audit_records)
            .where(audit_records.c.tenant_id == self._tenant_id)
            .order_by(audit_records.c.seq)
        ).mappings()
        chain = [
            StoredAuditRow(
                seq=r["seq"],
                audit_record_id=r["audit_record_id"],
                kind=r["kind"],
                decision_id=r["decision_id"],
                body=bytes(r["body"]),
                prev_hash=r["prev_hash"],
                record_hash=r["record_hash"],
            )
            for r in rows
        ]
        head = self._conn.execute(
            select(tenant_chain_heads.c.seq, tenant_chain_heads.c.record_hash).where(
                tenant_chain_heads.c.tenant_id == self._tenant_id
            )
        ).one_or_none()
        return chain, (0, ZERO_HASH) if head is None else (int(head.seq), str(head.record_hash))

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


def _mandate_row(m: Mandate) -> dict[str, object]:
    return {
        "tenant_id": m.tenant_id,
        "mandate_id": m.mandate_id,
        "payer_id": m.payer_id,
        "allowed_agent_ids": list(m.allowed_agent_ids),
        "currency": m.currency,
        "per_txn_cap_minor": m.per_txn_cap.minor,
        "monthly_cap_minor": m.monthly_cap.minor,
        "allowed_mccs": list(m.allowed_mccs),
        "valid_from": m.valid_from,
        "expires_at": m.expires_at,
        "supersedes": m.supersedes,
        "consent_method": m.consent_evidence.method,
        "consent_reference": m.consent_evidence.reference,
        "consent_captured_at": m.consent_evidence.captured_at,
        "created_at": m.created_at,
        "revoked_by": m.revoked_by,
        "revoked_at": m.revoked_at,
    }


def _mandate(row: RowMapping) -> Mandate:
    currency = row["currency"]
    return Mandate(
        mandate_id=row["mandate_id"],
        tenant_id=row["tenant_id"],
        payer_id=row["payer_id"],
        allowed_agent_ids=tuple(row["allowed_agent_ids"]),
        currency=currency,
        per_txn_cap=Money(minor=row["per_txn_cap_minor"], currency=currency),
        monthly_cap=Money(minor=row["monthly_cap_minor"], currency=currency),
        allowed_mccs=tuple(row["allowed_mccs"]),
        valid_from=row["valid_from"],
        expires_at=row["expires_at"],
        supersedes=row["supersedes"],
        consent_evidence=ConsentEvidence(
            method=row["consent_method"],
            reference=row["consent_reference"],
            captured_at=row["consent_captured_at"],
        ),
        created_at=row["created_at"],
        revoked_by=row["revoked_by"],
        revoked_at=row["revoked_at"],
    )


_conforms_uow: type[UnitOfWork] = PostgresStore  # mypy checks the adapter matches the ports
_conforms_directory: type[TenantDirectory] = PostgresStore
