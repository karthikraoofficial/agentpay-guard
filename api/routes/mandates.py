"""/v1/mandates: record, read, list and revoke Mandates. There is no update route:
terms change only through a Superseding Mandate (ADR-0001)."""

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from api.deps import ClockDep, IdemKey, ManageTenant, PackDep, StoreDep, Tenant
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from core.domain.mandates import (
    ConsentEvidence,
    Mandate,
    MandateId,
    MandateStatus,
    MandateTerms,
    RevokedBy,
)
from core.domain.money import Money
from core.domain.parties import AgentId, PayerId, new_id
from core.ports import TenantTransaction

router = APIRouter(prefix="/v1/mandates", tags=["mandates"])


class MandateCreate(MandateTerms):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revoked_by: RevokedBy


class MandateOut(BaseModel):
    mandate_id: MandateId
    status: MandateStatus
    payer_id: PayerId
    allowed_agent_ids: tuple[AgentId, ...]
    currency: str
    per_txn_cap: Money
    monthly_cap: Money
    allowed_mccs: tuple[str, ...]
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    supersedes: MandateId | None
    consent_evidence: ConsentEvidence
    created_at: AwareDatetime
    revoked_by: RevokedBy | None
    revoked_at: AwareDatetime | None


class MandatePage(BaseModel):
    data: list[MandateOut]
    next_cursor: str | None


def _out(mandate: Mandate, now: datetime) -> MandateOut:
    return MandateOut.model_validate(
        {**mandate.model_dump(exclude={"tenant_id"}), "status": mandate.status(now)}
    )


def _invalid(detail: str) -> ApiError:
    return ApiError(422, "VALIDATION_FAILED", detail)


def _check_references(tx: TenantTransaction, body: MandateCreate) -> None:
    """Every Payer, Agent and superseded Mandate named must exist in this Tenant."""
    if tx.get_payer(body.payer_id) is None:
        raise _invalid(f"No Payer {body.payer_id} in this Tenant.")
    missing = [a for a in body.allowed_agent_ids if tx.get_agent(a) is None]
    if missing:
        raise _invalid(f"No Agent {', '.join(missing)} in this Tenant.")
    if body.supersedes is not None:
        old = tx.get_mandate(body.supersedes)
        if old is None or old.payer_id != body.payer_id:
            raise _invalid(f"supersedes must name a Mandate of Payer {body.payer_id}.")


@router.post("", status_code=201, response_model=MandateOut)
def create_mandate(
    request: Request,
    body: MandateCreate,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    pack: PackDep,
    key: IdemKey,
) -> Response:
    if body.currency not in pack.pack.currencies:
        raise _invalid(f"Currency {body.currency} is not supported by the active Policy pack.")

    def write(now: datetime) -> tuple[int, MandateOut]:
        _check_references(tx, body)
        mandate = Mandate(
            **body.model_dump(),
            mandate_id=MandateId(new_id("mnd")),
            tenant_id=tenant,
            created_at=now,
        )
        tx.add_mandate(mandate)
        return 201, _out(mandate, now)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, body), clock, write)


@router.get("/{mandate_id}")
def get_mandate(mandate_id: str, tenant: Tenant, store: StoreDep, clock: ClockDep) -> MandateOut:
    with store.transaction(tenant) as tx:
        mandate = tx.get_mandate(MandateId(mandate_id))
    if mandate is None:
        raise ApiError(404, "MANDATE_NOT_FOUND", f"No Mandate {mandate_id}.")
    return _out(mandate, clock.now())


@router.get("")
def list_mandates(
    payer_id: str,
    tenant: Tenant,
    store: StoreDep,
    clock: ClockDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> MandatePage:
    """A Payer's Mandates, newest first. Pass `next_cursor` back to get the next page."""
    before = _decode(cursor) if cursor else None
    with store.transaction(tenant) as tx:
        rows = tx.list_mandates(PayerId(payer_id), limit + 1, before)
    now = clock.now()
    page = rows[:limit]
    more = len(rows) > limit
    return MandatePage(
        data=[_out(m, now) for m in page],
        next_cursor=_encode(page[-1]) if more else None,
    )


@router.post("/{mandate_id}/revoke", response_model=MandateOut)
def revoke_mandate(
    request: Request,
    mandate_id: str,
    body: RevokeBody,
    tenant: Tenant,
    store: StoreDep,
    clock: ClockDep,
    key: IdemKey,
) -> Response:
    """Permanently end a Mandate. Every Decision after this commits is blocked."""

    def write(now: datetime) -> tuple[int, MandateOut]:
        mid = MandateId(mandate_id)
        if tx.get_mandate(mid) is None:
            raise ApiError(404, "MANDATE_NOT_FOUND", f"No Mandate {mandate_id}.")
        if not tx.revoke_mandate(mid, body.revoked_by, now):
            raise ApiError(409, "MANDATE_ALREADY_REVOKED", f"Mandate {mandate_id} is revoked.")
        revoked = tx.get_mandate(mid)
        if revoked is None:  # read back inside the same transaction, so never None
            raise RuntimeError(f"Mandate {mandate_id} vanished mid-transaction")
        return 200, _out(revoked, now)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, body), clock, write)


class _Cursor(BaseModel):
    created_at: AwareDatetime
    mandate_id: MandateId


def _encode(last: Mandate) -> str:
    raw = _Cursor(created_at=last.created_at, mandate_id=last.mandate_id).model_dump_json()
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, MandateId]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        parsed = _Cursor.model_validate(json.loads(raw))
    except (binascii.Error, ValueError, ValidationError) as exc:
        raise _invalid("cursor is not one this API returned.") from exc
    return parsed.created_at, parsed.mandate_id
