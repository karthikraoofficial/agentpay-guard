"""/v1/consent-requests: the Tenant finds the Consent requests to put in front of its
Payers and records how they ended.

Listing and resolving need a `consent` key (ADR-0011); reading one works with any key,
so an Agent can check on its own step_up. A resolution is a new Audit record in the
Tenant's chain, written in one transaction with the Consent request locked first, then
the chain head (ADR-0009). The Decision itself never changes, and the first resolution
written wins."""

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from api.deps import ClockDep, ConsentTenant, IdemKey, StoreDep, Tenant
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from core.domain.audit import AuditRecord, seal
from core.domain.consent import (
    Attestation,
    ConsentRequest,
    ConsentRequestId,
    ConsentResolution,
    ConsentStatus,
    ResolutionEvidence,
    ResolutionStatus,
    ResolvedVia,
)
from core.domain.decisions import AuditRecordId, DecisionId
from core.domain.parties import new_id
from core.ports import TenantTransaction

router = APIRouter(prefix="/v1/consent-requests", tags=["consent"])


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


class ConsentResolutionOut(BaseModel):
    consent_request_id: ConsentRequestId
    decision_id: DecisionId
    status: ResolutionStatus
    resolved_at: AwareDatetime
    resolved_via: ResolvedVia
    audit_record_id: AuditRecordId


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
        raise ApiError(
            404, "CONSENT_REQUEST_NOT_FOUND", f"No Consent request {consent_request_id}."
        )
    return _out(consent, clock.now())


@router.post("/{consent_request_id}/decline", response_model=ConsentResolutionOut)
def decline_consent_request(
    request: Request,
    consent_request_id: str,
    tenant: ConsentTenant,
    store: StoreDep,
    clock: ClockDep,
    key: IdemKey,
    body: DeclineBody | None = None,
) -> Response:
    """The Payer said no. The attestation of how the Tenant confirmed it is optional."""
    attestation = body.attestation if body else None

    def write(now: datetime) -> tuple[int, ConsentResolutionOut]:
        consent = _open_for_resolution(tx, ConsentRequestId(consent_request_id), now)
        resolution = record_resolution(tx, consent, "declined", "tenant_api", attestation, now)
        return 200, _resolution_out(resolution)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, body), clock, write)


def _open_for_resolution(
    tx: TenantTransaction, consent_request_id: ConsentRequestId, now: datetime
) -> ConsentRequest:
    """Lock the request and check it can still be resolved at `now` (server time)."""
    consent = tx.lock_consent_request(consent_request_id)
    if consent is None:
        raise ApiError(
            404, "CONSENT_REQUEST_NOT_FOUND", f"No Consent request {consent_request_id}."
        )
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
) -> ConsentResolution:
    """Append the resolution's Audit record and store it for lookup. The caller holds
    the Consent request's lock; this takes the chain head."""
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
