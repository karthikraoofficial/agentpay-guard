from collections.abc import Iterator
from collections.abc import Set as AbstractSet
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import (
    ColumnElement,
    Connection,
    Engine,
    RowMapping,
    Select,
    and_,
    func,
    insert,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from adapters.postgres.tables import (
    agents,
    api_keys,
    audit_records,
    consent_links,
    consent_requests,
    consent_resolutions,
    decisions,
    idempotency_keys,
    intent_checks,
    mandates,
    outbox_events,
    payers,
    payments,
    tenant_chain_heads,
    tenants,
)
from core.domain.audit import ZERO_HASH, SealedAuditRecord, StoredAuditRow, unseal
from core.domain.consent import (
    Attestation,
    ConsentLink,
    ConsentRequest,
    ConsentRequestId,
    ConsentResolution,
    ConsentStatus,
)
from core.domain.decisions import AuditRecordId, Decision, DecisionId, PackRef
from core.domain.idempotency import IdempotencyKey, IdempotencyRecord
from core.domain.intents import IntentCheck, IntentContent, IntentId
from core.domain.mandates import ConsentEvidence, Mandate, MandateId, RevokedBy
from core.domain.money import Money
from core.domain.parties import (
    Agent,
    AgentId,
    AgentStatus,
    ApiKeyGrant,
    ApiKeyScope,
    Payer,
    PayerId,
)
from core.domain.payments import Payment, PaymentId
from core.domain.rails import RailToken
from core.ports import DomainEvent, EventBus, TenantDirectory, TenantId, UnitOfWork


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
                merchant_id=decision.merchant_id,
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
        d, r = decisions.c, consent_resolutions.c
        allowed = self._conn.execute(
            select(func.coalesce(func.sum(d.amount_minor), 0)).where(
                d.tenant_id == self._tenant_id,
                d.mandate_id == mandate_id,
                d.outcome == "allow",
                d.decided_at >= start,
                d.decided_at < end,
            )
        ).scalar_one()
        approved = self._conn.execute(
            select(func.coalesce(func.sum(d.amount_minor), 0))
            .select_from(
                consent_resolutions.join(
                    decisions, and_(d.tenant_id == r.tenant_id, d.decision_id == r.decision_id)
                )
            )
            .where(
                r.tenant_id == self._tenant_id,
                d.mandate_id == mandate_id,
                r.status == "approved",
                r.resolved_at >= start,
                r.resolved_at < end,
            )
        ).scalar_one()
        return int(allowed) + int(approved)

    def has_allowed_payment_to(self, payer_id: PayerId, merchant_id: str) -> bool:
        query = (
            select(decisions.c.decision_id)
            .join(
                mandates,
                (mandates.c.tenant_id == decisions.c.tenant_id)
                & (mandates.c.mandate_id == decisions.c.mandate_id),
            )
            .where(
                decisions.c.tenant_id == self._tenant_id,
                decisions.c.merchant_id == merchant_id,
                decisions.c.outcome == "allow",
                mandates.c.payer_id == payer_id,
            )
            .limit(1)
        )
        return self._conn.execute(query).first() is not None

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
        row = consent.model_dump(exclude={"resolution"})
        self._conn.execute(insert(consent_requests).values(**row))

    def get_consent_request(self, decision_id: DecisionId) -> ConsentRequest | None:
        query = _consents(self._tenant_id).where(consent_requests.c.decision_id == decision_id)
        row = self._conn.execute(query).mappings().one_or_none()
        return None if row is None else _consent(row)

    def find_consent_request(self, consent_request_id: ConsentRequestId) -> ConsentRequest | None:
        query = _consents(self._tenant_id).where(
            consent_requests.c.consent_request_id == consent_request_id
        )
        row = self._conn.execute(query).mappings().one_or_none()
        return None if row is None else _consent(row)

    def list_consent_requests(
        self,
        status: ConsentStatus | None,
        now: datetime,
        limit: int,
        before: tuple[datetime, ConsentRequestId] | None,
    ) -> list[ConsentRequest]:
        c, r = consent_requests.c, consent_resolutions.c
        query = _consents(self._tenant_id)
        unresolved = r.status.is_(None)
        if status == "pending":
            query = query.where(unresolved, c.expires_at > now)
        elif status == "expired":
            query = query.where(or_(and_(unresolved, c.expires_at <= now), r.status == "expired"))
        elif status is not None:
            query = query.where(r.status == status)
        if before is not None:
            query = query.where(tuple_(c.created_at, c.consent_request_id) < before)
        query = query.order_by(c.created_at.desc(), c.consent_request_id.desc()).limit(limit)
        return [_consent(row) for row in self._conn.execute(query).mappings()]

    def lock_consent_request(self, consent_request_id: ConsentRequestId) -> ConsentRequest | None:
        query = (
            _consents(self._tenant_id)
            .where(consent_requests.c.consent_request_id == consent_request_id)
            .with_for_update(of=consent_requests)
        )
        row = self._conn.execute(query).mappings().one_or_none()
        return None if row is None else _consent(row)

    def add_consent_link(self, link: ConsentLink) -> None:
        self._own(link.tenant_id)
        cl = consent_links.c
        self._conn.execute(
            update(consent_links)
            .where(
                cl.tenant_id == self._tenant_id,
                cl.consent_request_id == link.consent_request_id,
                cl.revoked_at.is_(None),
            )
            .values(revoked_at=link.created_at)
        )
        self._conn.execute(insert(consent_links).values(**link.model_dump()))

    def get_consent_link(self, token_hash: str) -> ConsentLink | None:
        cl = consent_links.c
        row = (
            self._conn.execute(
                select(consent_links).where(
                    cl.tenant_id == self._tenant_id, cl.token_hash == token_hash
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else ConsentLink.model_validate(dict(row))

    def publish(self, event: DomainEvent) -> None:
        self._own(event.tenant_id)
        self._conn.execute(
            pg_insert(outbox_events)
            .values(
                tenant_id=event.tenant_id,
                event_id=event.event_id,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                payload=dict(event.payload),
            )
            .on_conflict_do_nothing()
        )

    def add_consent_resolution(self, resolution: ConsentResolution) -> None:
        self._own(resolution.tenant_id)
        a = resolution.attestation
        self._conn.execute(
            insert(consent_resolutions).values(
                tenant_id=resolution.tenant_id,
                consent_request_id=resolution.consent_request_id,
                decision_id=resolution.decision_id,
                status=resolution.status,
                resolved_at=resolution.resolved_at,
                resolved_via=resolution.resolved_via,
                attestation_method=a.method if a else None,
                attestation_reference=a.reference if a else None,
                attestation_captured_at=a.captured_at if a else None,
                audit_record_id=resolution.audit_record_id,
                reason_codes=resolution.reason_codes,
            )
        )

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

    def add_payment(self, payment: Payment) -> None:
        self._own(payment.tenant_id)
        self._conn.execute(insert(payments).values(**_payment_row(payment)))

    def get_payment(self, payment_id: PaymentId) -> Payment | None:
        return self._payment(payments.c.payment_id == payment_id, lock=False)

    def get_payment_for_decision(self, decision_id: DecisionId) -> Payment | None:
        return self._payment(payments.c.decision_id == decision_id, lock=False)

    def lock_payment(self, payment_id: PaymentId) -> Payment | None:
        return self._payment(payments.c.payment_id == payment_id, lock=True)

    def update_payment(self, payment: Payment) -> None:
        self._own(payment.tenant_id)
        row = _payment_row(payment)
        changed = self._conn.execute(
            update(payments)
            .where(
                payments.c.tenant_id == self._tenant_id,
                payments.c.payment_id == payment.payment_id,
            )
            .values(
                status=row["status"],
                rail_payment_id=row["rail_payment_id"],
                failure_code=row["failure_code"],
                updated_at=row["updated_at"],
            )
        ).rowcount
        if changed != 1:
            raise ValueError(f"no Payment {payment.payment_id}")

    def _payment(self, where: ColumnElement[bool], *, lock: bool) -> Payment | None:
        query = select(payments).where(payments.c.tenant_id == self._tenant_id, where)
        row = (
            self._conn.execute(query.with_for_update() if lock else query).mappings().one_or_none()
        )
        if row is None:
            return None
        return Payment(
            payment_id=row["payment_id"],
            tenant_id=row["tenant_id"],
            decision_id=row["decision_id"],
            mandate_id=row["mandate_id"],
            amount=Money(minor=row["amount_minor"], currency=row["currency"]),
            rail=row["rail"],
            status=row["status"],
            rail_payment_id=row["rail_payment_id"],
            failure_code=row["failure_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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
            merchant_id=row["merchant_id"],
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

    def create_tenant(self, tenant_id: TenantId, name: str, created_at: datetime) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                insert(tenants).values(tenant_id=tenant_id, name=name, created_at=created_at)
            )

    def add_api_key(
        self,
        tenant_id: TenantId,
        key_prefix: str,
        key_hash: str,
        scopes: AbstractSet[ApiKeyScope],
        created_at: datetime,
    ) -> bool:
        with self._engine.begin() as conn:
            exists = conn.execute(
                select(tenants.c.tenant_id).where(tenants.c.tenant_id == tenant_id)
            ).scalar_one_or_none()
            if exists is None:
                return False
            conn.execute(
                insert(api_keys).values(
                    key_hash=key_hash,
                    tenant_id=tenant_id,
                    key_prefix=key_prefix,
                    scopes=sorted(scopes),
                    created_at=created_at,
                )
            )
        return True

    def api_key(self, key_hash: str) -> ApiKeyGrant | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(api_keys.c.tenant_id, api_keys.c.scopes).where(
                    api_keys.c.key_hash == key_hash
                )
            ).one_or_none()
        if row is None:
            return None
        return ApiKeyGrant(tenant_id=TenantId(row.tenant_id), scopes=frozenset(row.scopes))

    def consent_link(self, token_hash: str) -> ConsentLink | None:
        query = select(consent_links).where(consent_links.c.token_hash == token_hash)
        with self._engine.connect() as conn:
            row = conn.execute(query).mappings().one_or_none()
        return None if row is None else ConsentLink.model_validate(dict(row))

    def undelivered(self, limit: int) -> list[DomainEvent]:
        o = outbox_events.c
        query = (
            select(outbox_events)
            .where(o.delivered_at.is_(None))
            .order_by(o.occurred_at, o.event_id)
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(query).mappings().all()
        return [
            DomainEvent(
                event_id=r["event_id"],
                tenant_id=TenantId(r["tenant_id"]),
                event_type=r["event_type"],
                occurred_at=r["occurred_at"],
                payload=r["payload"],
            )
            for r in rows
        ]

    def mark_delivered(self, tenant_id: TenantId, event_id: str, at: datetime) -> None:
        o = outbox_events.c
        with self._engine.begin() as conn:
            conn.execute(
                update(outbox_events)
                .where(o.tenant_id == tenant_id, o.event_id == event_id)
                .values(delivered_at=at)
            )


def _consents(tenant_id: TenantId) -> Select[tuple[object, ...]]:
    """A Tenant's Consent requests, each joined to its resolution if it has one."""
    c, r = consent_requests.c, consent_resolutions.c
    joined = consent_requests.outerjoin(
        consent_resolutions,
        and_(r.tenant_id == c.tenant_id, r.consent_request_id == c.consent_request_id),
    )
    return (
        select(
            c.consent_request_id,
            c.tenant_id,
            c.decision_id,
            c.created_at,
            c.expires_at,
            r.status,
            r.resolved_at,
            r.resolved_via,
            r.attestation_method,
            r.attestation_reference,
            r.attestation_captured_at,
            r.audit_record_id,
            r.reason_codes,
        )
        .select_from(joined)
        .where(c.tenant_id == tenant_id)
    )


def _consent(row: RowMapping) -> ConsentRequest:
    resolution = None
    if row["status"] is not None:
        attestation = None
        if row["attestation_method"] is not None:
            attestation = Attestation(
                method=row["attestation_method"],
                reference=row["attestation_reference"],
                captured_at=row["attestation_captured_at"],
            )
        resolution = ConsentResolution(
            consent_request_id=row["consent_request_id"],
            tenant_id=row["tenant_id"],
            decision_id=row["decision_id"],
            status=row["status"],
            resolved_at=row["resolved_at"],
            resolved_via=row["resolved_via"],
            attestation=attestation,
            audit_record_id=row["audit_record_id"],
            reason_codes=None if row["reason_codes"] is None else tuple(row["reason_codes"]),
        )
    return ConsentRequest(
        consent_request_id=row["consent_request_id"],
        tenant_id=row["tenant_id"],
        decision_id=row["decision_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        resolution=resolution,
    )


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
        "rail": m.rail_token.rail if m.rail_token else None,
        "rail_customer_ref": m.rail_token.customer_ref if m.rail_token else None,
        "rail_token_ref": m.rail_token.token_ref if m.rail_token else None,
        "rail_display": m.rail_token.display if m.rail_token else None,
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
        rail_token=None
        if row["rail"] is None
        else RailToken(
            rail=row["rail"],
            customer_ref=row["rail_customer_ref"],
            token_ref=row["rail_token_ref"],
            display=row["rail_display"],
        ),
    )


def _payment_row(payment: Payment) -> dict[str, object]:
    return {
        "tenant_id": payment.tenant_id,
        "payment_id": payment.payment_id,
        "decision_id": payment.decision_id,
        "mandate_id": payment.mandate_id,
        "amount_minor": payment.amount.minor,
        "currency": payment.amount.currency,
        "rail": payment.rail,
        "status": payment.status,
        "rail_payment_id": payment.rail_payment_id,
        "failure_code": payment.failure_code,
        "created_at": payment.created_at,
        "updated_at": payment.updated_at,
    }


_conforms_uow: type[UnitOfWork] = PostgresStore  # mypy checks the adapter matches the ports
_conforms_directory: type[TenantDirectory] = PostgresStore
_conforms_bus: type[EventBus] = PostgresStore
