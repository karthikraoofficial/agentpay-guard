"""/v1/disputes: a Tenant reports that the Payer has disputed a captured Payment, and
reads the Evidence pack built for it (ADR-0018).

Only a `manage` key may open or read one: an Agent never argues its own case. The pack
is built after the request, by the `dispute_pack` workflow the outbox starts, so a new
Dispute is `open` with no pack until then."""

from datetime import datetime

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from api.deps import ClockDep, IdemKey, ManageTenant, SettingsDep, StoreDep
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from core.disputes import (
    dispute_refusal,
    open_dispute,
    request_regenerate,
    sign_off,
    withdraw,
)
from core.domain.decisions import DecisionId, decision_time
from core.domain.disputes import (
    PAYER_STATEMENT_MAX,
    Dispute,
    DisputeId,
    DisputeReason,
    DisputeStatus,
    Reviewer,
    SignOff,
    regenerate_refusal,
    sign_off_refusal,
    withdraw_refusal,
)
from core.domain.evidence import EvidencePack
from core.domain.payments import Payment, PaymentId
from core.ports import TenantTransaction

router = APIRouter(prefix="/v1/disputes", tags=["disputes"])


class OpenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_id: PaymentId
    reason: DisputeReason
    # The network's deadline for the Tenant's response.
    respond_by: AwareDatetime
    # The Payer's own words, if the Tenant has them. Never put in the Audit chain.
    payer_statement: str | None = Field(default=None, min_length=1, max_length=PAYER_STATEMENT_MAX)


class DisputeOut(BaseModel):
    dispute_id: DisputeId
    payment_id: PaymentId
    decision_id: DecisionId
    reason: DisputeReason
    status: DisputeStatus
    respond_by: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime
    # Who signed off which pack version, once the Dispute is ready.
    sign_off: SignOff | None
    # The latest Evidence pack, once one is built, and its pack hash: what a Sign-off
    # approves.
    pack: EvidencePack | None
    pack_hash: str | None


class SignOffBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The version the reviewer read; refused unless it is still the latest.
    pack_version: int = Field(ge=1)
    reviewer: Reviewer


def _out(dispute: Dispute, pack: EvidencePack | None) -> DisputeOut:
    return DisputeOut.model_validate(
        dispute.model_dump(include=set(DisputeOut.model_fields))
        | {"pack": pack, "pack_hash": None if pack is None else pack.hash()}
    )


@router.post("", status_code=201, response_model=DisputeOut)
def create_dispute(
    request: Request,
    body: OpenBody,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    key: IdemKey,
) -> Response:
    fingerprint = request_hash(request, body)

    def write(read_at: datetime) -> tuple[int, DisputeOut]:
        now = decision_time(read_at)
        if body.respond_by <= now:
            raise ApiError(422, "RESPOND_BY_PASSED", "respond_by is already in the past.")
        found = tx.get_payment(body.payment_id)
        if found is None:
            raise ApiError(404, "PAYMENT_NOT_FOUND", f"No Payment {body.payment_id}.")
        # Mandate before Payment, as rail reports take them, so the two queue.
        tx.lock_mandate(found.mandate_id)
        payment = _locked(tx, body.payment_id)
        refusal = dispute_refusal(
            payment.status, tx.get_dispute_for_payment(payment.payment_id) is not None
        )
        if refusal is not None:
            raise ApiError(409, refusal, _REFUSAL_TEXT[refusal])
        dispute = open_dispute(tx, payment, body.reason, body.respond_by, body.payer_statement, now)
        return 201, _out(dispute, None)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, fingerprint, clock, write)


@router.post("/{dispute_id}/sign-off", response_model=DisputeOut)
def sign_off_dispute(
    request: Request,
    dispute_id: str,
    body: SignOffBody,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    key: IdemKey,
) -> Response:
    """A named person at the Tenant approves the current pack version: its pack hash goes
    into the Audit chain and the Dispute becomes ready."""
    fingerprint = request_hash(request, body)

    def write(read_at: datetime) -> tuple[int, DisputeOut]:
        now = decision_time(read_at)
        dispute = _locked_dispute(tx, dispute_id)
        pack = tx.get_evidence_pack(dispute.dispute_id)
        refusal = sign_off_refusal(
            dispute.status, None if pack is None else pack.version, body.pack_version
        )
        if refusal is not None:
            raise ApiError(409, refusal, _REFUSAL_TEXT[refusal])
        if pack is None:  # sign_off_refusal refuses this; keeps mypy sure
            raise RuntimeError("a Dispute in review has a pack")
        return 200, _out(sign_off(tx, dispute, pack, body.reviewer, now), pack)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, fingerprint, clock, write)


@router.post("/{dispute_id}/withdraw", response_model=DisputeOut)
def withdraw_dispute(
    request: Request,
    dispute_id: str,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    key: IdemKey,
) -> Response:
    """Close a Dispute the Tenant no longer needs, such as one the Payer dropped."""

    def write(read_at: datetime) -> tuple[int, DisputeOut]:
        now = decision_time(read_at)
        dispute = _locked_dispute(tx, dispute_id)
        refusal = withdraw_refusal(dispute.status)
        if refusal is not None:
            raise ApiError(409, refusal, _REFUSAL_TEXT[refusal])
        return 200, _out(withdraw(tx, dispute, now), tx.get_evidence_pack(dispute.dispute_id))

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, None), clock, write)


@router.post("/{dispute_id}/regenerate", response_model=DisputeOut)
def regenerate_pack(
    request: Request,
    dispute_id: str,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    settings: SettingsDep,
    key: IdemKey,
) -> Response:
    """Ask for the next pack version: the Dispute is `open` until it is built, and that
    version needs its own Sign-off."""

    def write(read_at: datetime) -> tuple[int, DisputeOut]:
        now = decision_time(read_at)
        dispute = _locked_dispute(tx, dispute_id)
        pack = tx.get_evidence_pack(dispute.dispute_id)
        refusal = regenerate_refusal(
            dispute.status,
            None if pack is None else pack.version,
            settings.dispute_pack_versions_max,
        )
        if refusal is not None:
            raise ApiError(409, refusal, _REFUSAL_TEXT[refusal])
        return 200, _out(request_regenerate(tx, dispute, now), pack)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, None), clock, write)


@router.get("/{dispute_id}")
def get_dispute(dispute_id: str, tenant: ManageTenant, store: StoreDep) -> DisputeOut:
    with store.transaction(tenant) as tx:
        dispute = tx.get_dispute(DisputeId(dispute_id))
        pack = None if dispute is None else tx.get_evidence_pack(dispute.dispute_id)
    if dispute is None:
        raise ApiError(404, "DISPUTE_NOT_FOUND", f"No Dispute {dispute_id}.")
    return _out(dispute, pack)


def _locked_dispute(tx: TenantTransaction, dispute_id: str) -> Dispute:
    dispute = tx.lock_dispute(DisputeId(dispute_id))
    if dispute is None:
        raise ApiError(404, "DISPUTE_NOT_FOUND", f"No Dispute {dispute_id}.")
    return dispute


def _locked(tx: TenantTransaction, payment_id: PaymentId) -> Payment:
    payment = tx.lock_payment(payment_id)
    if payment is None:
        raise RuntimeError(f"Payment {payment_id} vanished")
    return payment


_REFUSAL_TEXT = {
    "PAYMENT_NOT_DISPUTABLE": "Only a captured Payment can be disputed.",
    "PAYMENT_ALREADY_DISPUTED": "This Payment already has a Dispute.",
    "DISPUTE_NOT_IN_REVIEW": "Only a Dispute whose pack is built and not yet signed off "
    "can be signed off.",
    "PACK_VERSION_NOT_CURRENT": "That pack version is not the latest; review the latest one.",
    "DISPUTE_CLOSED": "The Dispute is signed off, expired or withdrawn.",
    "PACK_VERSION_LIMIT": "This Dispute has as many pack versions as it may have.",
}
