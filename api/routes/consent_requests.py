"""/v1/consent-requests: the Tenant finds the Consent requests to put in front of its
Payers and records how they ended.

Listing and resolving need a `consent` key (ADR-0011); reading one works with any key,
so an Agent can check on its own step_up. A resolution is a new Audit record in the
Tenant's chain, written in one transaction with the Consent request locked first, then
the chain head (ADR-0009). The Decision itself never changes, and the first resolution
written wins. Approval locks the Mandate before the Consent request and re-runs the
engine on fresh inputs, so it can lift a step_up but never a block (ADR-0010)."""

import base64
import binascii
import json
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from api.deps import ClockDep, ConsentTenant, IdemKey, PackDep, StoreDep, Tenant, WorkflowDep
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from api.routes.decisions import decision_time
from core.domain.audit import AuditRecord, DecisionInputs, seal
from core.domain.consent import (
    Attestation,
    ConsentRequest,
    ConsentRequestId,
    ConsentResolution,
    ConsentStatus,
    ResolutionEvidence,
    ResolutionStatus,
    ResolvedVia,
    approval_result,
)
from core.domain.decisions import (
    AuditRecordId,
    DecisionId,
    EngineOutput,
    PackRef,
    ReasonCode,
    month_window,
)
from core.domain.money import Money
from core.domain.parties import new_id
from core.policy.engine import decide
from core.policy.packs import LoadedPack
from core.ports import TenantId, TenantTransaction, Workflow

router = APIRouter(prefix="/v1/consent-requests", tags=["consent"])
log = logging.getLogger(__name__)


class ConsentRequestOut(BaseModel):
    consent_request_id: ConsentRequestId
    decision_id: DecisionId
    status: ConsentStatus
    created_at: AwareDatetime
    expires_at: AwareDatetime


class ConsentRequestPage(BaseModel):
    data: list[ConsentRequestOut]
    next_cursor: str | None


class DeclineBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attestation: Attestation | None = None


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attestation: Attestation


class ConsentResolutionOut(BaseModel):
    consent_request_id: ConsentRequestId
    decision_id: DecisionId
    status: ResolutionStatus
    resolved_at: AwareDatetime
    resolved_via: ResolvedVia
    audit_record_id: AuditRecordId
    # Only when an approval was blocked: the re-check's Reason codes.
    reason_codes: tuple[ReasonCode, ...] | None


def _out(consent: ConsentRequest, now: datetime) -> ConsentRequestOut:
    return ConsentRequestOut(
        consent_request_id=consent.consent_request_id,
        decision_id=consent.decision_id,
        status=consent.status(now),
        created_at=consent.created_at,
        expires_at=consent.expires_at,
    )


@router.get("")
def list_consent_requests(
    tenant: ConsentTenant,
    store: StoreDep,
    clock: ClockDep,
    status: ConsentStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> ConsentRequestPage:
    """The Tenant's Consent requests, newest first, optionally with one status (worked
    out when read). Pass `next_cursor` back to get the next page."""
    before = _decode(cursor) if cursor else None
    now = clock.now()
    with store.transaction(tenant) as tx:
        rows = tx.list_consent_requests(status, now, limit + 1, before)
    page = rows[:limit]
    return ConsentRequestPage(
        data=[_out(c, now) for c in page],
        next_cursor=_encode(page[-1]) if len(rows) > limit else None,
    )


@router.get("/{consent_request_id}")
def get_consent_request(
    consent_request_id: str, tenant: Tenant, store: StoreDep, clock: ClockDep
) -> ConsentRequestOut:
    with store.transaction(tenant) as tx:
        consent = tx.find_consent_request(ConsentRequestId(consent_request_id))
    if consent is None:
        raise _not_found(consent_request_id)
    return _out(consent, clock.now())


@router.post("/{consent_request_id}/decline", response_model=ConsentResolutionOut)
def decline_consent_request(
    request: Request,
    consent_request_id: str,
    tenant: ConsentTenant,
    store: StoreDep,
    clock: ClockDep,
    workflow: WorkflowDep,
    key: IdemKey,
    body: DeclineBody | None = None,
) -> Response:
    """The Payer said no. The attestation of how the Tenant confirmed it is optional."""
    attestation = body.attestation if body else None

    def write(read_at: datetime) -> tuple[int, ConsentResolutionOut]:
        csr = ConsentRequestId(consent_request_id)
        resolution = decline(tx, csr, "tenant_api", attestation, decision_time(read_at))
        return 200, _resolution_out(resolution)

    with store.transaction(tenant) as tx:
        response = run_idempotent(tx, key, request_hash(request, body), clock, write)
    signal_resolved(workflow, tenant, ConsentRequestId(consent_request_id))
    return response


@router.post("/{consent_request_id}/approve", response_model=ConsentResolutionOut)
def approve_consent_request(
    request: Request,
    consent_request_id: str,
    body: ApproveBody,
    tenant: ConsentTenant,
    store: StoreDep,
    clock: ClockDep,
    pack: PackDep,
    workflow: WorkflowDep,
    key: IdemKey,
) -> Response:
    """The Payer said yes. Returns 200 with `approved`, or `blocked` with the Reason
    codes if the Mandate no longer permits the payment."""

    def write(read_at: datetime) -> tuple[int, ConsentResolutionOut]:
        csr = ConsentRequestId(consent_request_id)
        now = decision_time(read_at)
        resolution = approve(tx, csr, "tenant_api", body.attestation, now, pack)
        return 200, _resolution_out(resolution)

    with store.transaction(tenant) as tx:
        response = run_idempotent(tx, key, request_hash(request, body), clock, write)
    signal_resolved(workflow, tenant, ConsentRequestId(consent_request_id))
    return response


def signal_resolved(
    workflow: Workflow, tenant_id: TenantId, consent_request_id: ConsentRequestId
) -> None:
    """Tell the workflow to stop waiting, after the resolution has committed. Best effort:
    Postgres already holds the answer, and the workflow's timer finds it if this fails."""
    try:
        workflow.signal_resolved(tenant_id, consent_request_id)
    except Exception as exc:  # never fail a stored resolution over a nudge
        log.warning(
            "consent workflow %s for Tenant %s not signalled: %s",
            consent_request_id,
            tenant_id,
            type(exc).__name__,
        )


def decline(
    tx: TenantTransaction,
    consent_request_id: ConsentRequestId,
    via: ResolvedVia,
    attestation: Attestation | None,
    now: datetime,
) -> ConsentResolution:
    consent = _open_for_resolution(tx, consent_request_id, now)
    return record_resolution(tx, consent, "declined", via, attestation, now)


def approve(
    tx: TenantTransaction,
    consent_request_id: ConsentRequestId,
    via: ResolvedVia,
    attestation: Attestation,
    now: datetime,
    pack: LoadedPack,
) -> ConsentResolution:
    """Re-run the engine on the request as it was asked, with the Signals recorded then,
    against the Mandate, Agent and Spent this month as they are now (ADR-0010)."""
    found = tx.find_consent_request(consent_request_id)
    if found is None:
        raise _not_found(consent_request_id)
    decision = tx.get_decision(found.decision_id)
    original = tx.get_audit_record(decision.audit_record_id) if decision else None
    asked = original.record.inputs if original else None
    if decision is None or asked is None:
        raise RuntimeError(f"Consent request {consent_request_id} has no recorded Decision")
    # Mandate first, as a Decision does, so approvals and Decisions on one Mandate queue.
    mandate = tx.lock_mandate(decision.mandate_id)
    if mandate is None:
        raise RuntimeError(f"Mandate {decision.mandate_id} vanished")
    consent = _open_for_resolution(tx, consent_request_id, now)
    spent = Money(
        minor=tx.allowed_spend(mandate.mandate_id, *month_window(now)),
        currency=mandate.currency,
    )
    agent = tx.get_agent(decision.agent_id)
    inputs = DecisionInputs(
        request=asked.request,
        mandate=mandate,
        agent=agent,
        spent_this_month=spent,
        signals=asked.signals,
        now=now,
    )
    output = decide(asked.request, mandate, agent, spent, asked.signals, pack.pack, now)
    ref = PackRef(id=pack.pack.id, version=pack.pack.version, hash=pack.hash)
    status = approval_result(output)
    return record_resolution(tx, consent, status, via, attestation, now, run=(inputs, ref, output))


def _not_found(consent_request_id: str) -> ApiError:
    return ApiError(404, "CONSENT_REQUEST_NOT_FOUND", f"No Consent request {consent_request_id}.")


def _open_for_resolution(
    tx: TenantTransaction, consent_request_id: ConsentRequestId, now: datetime
) -> ConsentRequest:
    """Lock the request and check it can still be resolved at `now` (server time)."""
    consent = tx.lock_consent_request(consent_request_id)
    if consent is None:
        raise _not_found(consent_request_id)
    if consent.resolution is not None:
        raise ApiError(
            409,
            "CONSENT_ALREADY_RESOLVED",
            f"Consent request {consent_request_id} is already {consent.resolution.status}.",
        )
    if now >= consent.expires_at:
        raise ApiError(409, "CONSENT_EXPIRED", f"Consent request {consent_request_id} expired.")
    return consent


def record_resolution(
    tx: TenantTransaction,
    consent: ConsentRequest,
    status: ResolutionStatus,
    via: ResolvedVia,
    attestation: Attestation | None,
    now: datetime,
    *,
    run: tuple[DecisionInputs, PackRef, EngineOutput] | None = None,
) -> ConsentResolution:
    """Append the resolution's Audit record and store it for lookup. The caller holds
    the Consent request's lock; this takes the chain head. `run` is an approval's engine
    re-run, recorded so the resolution replays like a Decision."""
    inputs, ref, output = run if run else (None, None, None)
    seq, prev_hash = tx.lock_chain_head()
    resolution = ConsentResolution(
        consent_request_id=consent.consent_request_id,
        tenant_id=consent.tenant_id,
        decision_id=consent.decision_id,
        status=status,
        resolved_at=now,
        resolved_via=via,
        attestation=attestation,
        audit_record_id=AuditRecordId(new_id("aud")),
        reason_codes=output.reason_codes if output and status == "blocked" else None,
    )
    sealed = seal(
        AuditRecord(
            audit_record_id=resolution.audit_record_id,
            tenant_id=consent.tenant_id,
            seq=seq + 1,
            prev_hash=prev_hash,
            kind="consent_resolution",
            decision_id=consent.decision_id,
            recorded_at=now,
            inputs=inputs,
            pack=ref,
            output=output,
            resolution=ResolutionEvidence(
                consent_request_id=consent.consent_request_id,
                status=status,
                resolved_via=via,
                attestation=attestation,
            ),
        )
    )
    tx.add_consent_resolution(resolution)
    tx.append_audit_record(sealed)
    return resolution


def _resolution_out(resolution: ConsentResolution) -> ConsentResolutionOut:
    return ConsentResolutionOut.model_validate(
        resolution.model_dump(include=set(ConsentResolutionOut.model_fields))
    )


class _Cursor(BaseModel):
    created_at: AwareDatetime
    consent_request_id: ConsentRequestId


def _encode(last: ConsentRequest) -> str:
    raw = _Cursor(
        created_at=last.created_at, consent_request_id=last.consent_request_id
    ).model_dump_json()
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, ConsentRequestId]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        parsed = _Cursor.model_validate(json.loads(raw))
    except (binascii.Error, ValueError, ValidationError) as exc:
        raise ApiError(422, "VALIDATION_FAILED", "cursor is not one this API returned.") from exc
    return parsed.created_at, parsed.consent_request_id
