"""/v1/disputes: a Tenant reports that the Payer has disputed a captured Payment, and
reads the Evidence pack built for it (ADR-0018).

Only a `manage` key may open or read one: an Agent never argues its own case. The pack
is built after the request, by the `dispute_pack` workflow the outbox starts, so a new
Dispute is `open` with no pack until then."""

from datetime import datetime

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from api.deps import ClockDep, IdemKey, ManageTenant, StoreDep
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from core.disputes import dispute_refusal, open_dispute
from core.domain.decisions import DecisionId, decision_time
from core.domain.disputes import (
    PAYER_STATEMENT_MAX,
    Dispute,
    DisputeId,
    DisputeReason,
    DisputeStatus,
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
    # The latest Evidence pack, once one is built.
    pack: EvidencePack | None


def _out(dispute: Dispute, pack: EvidencePack | None) -> DisputeOut:
    return DisputeOut.model_validate(
        dispute.model_dump(include=set(DisputeOut.model_fields)) | {"pack": pack}
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


@router.get("/{dispute_id}")
def get_dispute(dispute_id: str, tenant: ManageTenant, store: StoreDep) -> DisputeOut:
    with store.transaction(tenant) as tx:
        dispute = tx.get_dispute(DisputeId(dispute_id))
        pack = None if dispute is None else tx.get_evidence_pack(dispute.dispute_id)
    if dispute is None:
        raise ApiError(404, "DISPUTE_NOT_FOUND", f"No Dispute {dispute_id}.")
    return _out(dispute, pack)


def _locked(tx: TenantTransaction, payment_id: PaymentId) -> Payment:
    payment = tx.lock_payment(payment_id)
    if payment is None:
        raise RuntimeError(f"Payment {payment_id} vanished")
    return payment


_REFUSAL_TEXT = {
    "PAYMENT_NOT_DISPUTABLE": "Only a captured Payment can be disputed.",
    "PAYMENT_ALREADY_DISPUTED": "This Payment already has a Dispute.",
}
