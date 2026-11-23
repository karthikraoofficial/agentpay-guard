"""The inspector's read endpoints (#275), with the `manage` scope and for the caller's
Tenant only. They share their checks with `api.admin replay` and `verify`.

- `GET /v1/audit-records/{id}`: what the record decided and why: its kind, Outcome, Reason
  codes, Policy pack and Rule results. It leaves out the recorded inputs.
- `GET /v1/audit-records/{id}/replay`: re-run the record's engine run and compare bytes.
- `GET /v1/audit-chain/verify`: walk the Tenant's Audit chain.
"""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from api.deps import ManageTenant, SettingsDep, StoreDep
from api.errors import ApiError
from core.audit_inspection import ReplayStatus, replay_record, verify_tenant_chain
from core.domain.audit import SealedAuditRecord
from core.domain.decisions import AuditRecordId, PackRef, RuleResult
from core.ports import TenantId, UnitOfWork

router = APIRouter(prefix="/v1", tags=["audit"])


class AuditRecordOut(BaseModel):
    audit_record_id: str
    seq: int
    kind: str
    decision_id: str
    recorded_at: datetime
    record_hash: str
    # Only on a record with an engine run.
    policy_pack: PackRef | None
    outcome: str | None
    reason_codes: list[str]
    rule_results: list[RuleResult]


class ReplayOut(BaseModel):
    audit_record_id: str
    seq: int
    status: ReplayStatus
    identical: bool
    difference: str | None
    # What kind of record has no engine run, or why the recorded pack can't be used.
    detail: str | None


class ChainOut(BaseModel):
    clean: bool
    record_count: int
    first_broken_seq: int | None


def _sealed(store: UnitOfWork, tenant: TenantId, audit_record_id: str) -> SealedAuditRecord:
    with store.transaction(tenant) as tx:
        sealed = tx.get_audit_record(AuditRecordId(audit_record_id))
    if sealed is None:
        raise ApiError(404, "NOT_FOUND", f"No Audit record {audit_record_id}.")
    return sealed


@router.get("/audit-records/{audit_record_id}", response_model=AuditRecordOut)
def read_audit_record(
    audit_record_id: str, tenant: ManageTenant, store: StoreDep
) -> AuditRecordOut:
    sealed = _sealed(store, tenant, audit_record_id)
    record, output = sealed.record, sealed.record.output
    return AuditRecordOut(
        audit_record_id=record.audit_record_id,
        seq=record.seq,
        kind=record.kind,
        decision_id=record.decision_id,
        recorded_at=record.recorded_at,
        record_hash=sealed.record_hash,
        policy_pack=record.pack,
        outcome=output.outcome if output else None,
        reason_codes=list(output.reason_codes) if output else [],
        rule_results=list(output.rule_results) if output else [],
    )


@router.get("/audit-records/{audit_record_id}/replay", response_model=ReplayOut)
def replay_audit_record(
    audit_record_id: str, tenant: ManageTenant, store: StoreDep, settings: SettingsDep
) -> ReplayOut:
    report = replay_record(_sealed(store, tenant, audit_record_id), settings.policy_packs_dir)
    return ReplayOut(
        audit_record_id=report.audit_record_id,
        seq=report.seq,
        status=report.status,
        identical=report.identical,
        difference=report.difference,
        detail=report.detail,
    )


@router.get("/audit-chain/verify", response_model=ChainOut)
def verify_audit_chain(tenant: ManageTenant, store: StoreDep) -> ChainOut:
    with store.transaction(tenant) as tx:
        report = verify_tenant_chain(tx)
    return ChainOut(
        clean=report.clean,
        record_count=report.record_count,
        first_broken_seq=report.first_broken_seq,
    )
