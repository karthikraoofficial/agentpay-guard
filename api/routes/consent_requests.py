"""/v1/consent-requests: the Tenant finds the Consent requests to put in front of its
Payers. Listing needs a `consent` key (ADR-0011); reading one works with any key, so an
Agent can check on its own step_up. Status is worked out when read, never stored."""

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import AwareDatetime, BaseModel, ValidationError

from api.deps import ClockDep, ConsentTenant, StoreDep, Tenant
from api.errors import ApiError
from core.domain.consent import (
    ConsentRequest,
    ConsentRequestId,
    ConsentStatus,
    ListableConsentStatus,
)
from core.domain.decisions import DecisionId

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
    status: ListableConsentStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> ConsentRequestPage:
    """The Tenant's Consent requests, newest first, optionally only `pending` or
    `expired`. Pass `next_cursor` back to get the next page."""
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
