"""/v1/payments: an Agent pays an allowed Decision, or an approved step-up.

The only path to the rail. In order (ADR-0014):

1. One transaction locks the Mandate, checks the Decision is payable, and writes the
   Payment, its `payment` Audit record and the idempotency entry together.
2. The rail is asked to charge, keyed by the Payment id, so asking again never makes a
   second charge.
3. A second transaction records what the rail said, and keeps it as the response for
   replays of the Idempotency-Key.

If the rail's answer never arrived (a timeout, or a crash after step 1), the Payment stays
scheduled with no rail payment id. A retry with the same key then asks the rail again
rather than replaying: the rail finds the charge by Payment id, so it is never doubled.

Refusals are 409s, logged and traced but not audited, and never reach the rail.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict

from api.deps import (
    ClockDep,
    DecideTenant,
    IdemKey,
    PackDep,
    RailDep,
    SettingsDep,
    StoreDep,
    TelemetryDep,
    Tenant,
)
from api.errors import ApiError
from api.idempotency import REPLAYED, request_hash, run_idempotent
from api.payability import payable_until
from core.charges import apply_charge, record_payment
from core.domain.decisions import DecisionId, decision_time
from core.domain.idempotency import IdempotencyKey, IdempotencyRecord
from core.domain.mandates import MandateId
from core.domain.money import Money
from core.domain.parties import new_id
from core.domain.payments import Payment, PaymentId, PaymentStatus, payment_refusal
from core.domain.rails import RailName, RailToken
from core.ports import PaymentRail, RailUnavailable, TenantId

router = APIRouter(prefix="/v1/payments", tags=["payments"])
log = logging.getLogger(__name__)


class PayBody(BaseModel):
    """Only the Decision: the amount and the instrument come from it and its Mandate."""

    model_config = ConfigDict(extra="forbid")

    decision_id: DecisionId


class PaymentOut(BaseModel):
    payment_id: PaymentId
    decision_id: DecisionId
    mandate_id: MandateId
    amount: Money
    rail: RailName
    status: PaymentStatus
    rail_payment_id: str | None
    failure_code: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


def _out(payment: Payment) -> PaymentOut:
    return PaymentOut.model_validate(payment.model_dump(include=set(PaymentOut.model_fields)))


@router.post("", status_code=202, response_model=PaymentOut)
def create_payment(
    request: Request,
    body: PayBody,
    tenant: DecideTenant,
    store: StoreDep,
    clock: ClockDep,
    pack: PackDep,
    settings: SettingsDep,
    rail: RailDep,
    telemetry: TelemetryDep,
    key: IdemKey,
) -> Response:
    fingerprint = request_hash(request, body)
    made: list[tuple[Payment, RailToken]] = []

    def write(read_at: datetime) -> tuple[int, PaymentOut]:
        now = decision_time(read_at)
        decision = tx.get_decision(body.decision_id)
        if decision is None:
            raise ApiError(404, "DECISION_NOT_FOUND", f"No Decision {body.decision_id}.")
        span.set_attribute("decision_id", decision.decision_id)
        # Mandate first, as Decisions and approvals do, so payments of one Decision queue.
        mandate = tx.lock_mandate(decision.mandate_id)
        if mandate is None:
            raise RuntimeError(f"Mandate {decision.mandate_id} vanished")
        agent = tx.get_agent(decision.agent_id)
        consent = tx.get_consent_request(decision.decision_id)
        refusal = payment_refusal(
            payable_until=payable_until(tx, decision, consent, pack, settings.policy_packs_dir),
            already_paid=tx.get_payment_for_decision(decision.decision_id) is not None,
            mandate_status=mandate.status(now),
            agent_disabled=agent is None or agent.status == "disabled",
            rail_token=mandate.rail_token,
            rail=settings.rail,
            now=now,
        )
        if refusal is not None:
            span.set_attribute("refusal_code", refusal)
            log.info(
                "payment refused %s: Decision %s, Tenant %s", refusal, decision.decision_id, tenant
            )
            raise ApiError(409, refusal, _REFUSAL_TEXT[refusal])
        if mandate.rail_token is None:  # payment_refusal refuses this; keeps mypy sure
            raise RuntimeError("a payable Mandate has a Rail token")
        payment = Payment(
            payment_id=PaymentId(new_id("pmt")),
            tenant_id=tenant,
            decision_id=decision.decision_id,
            mandate_id=mandate.mandate_id,
            amount=decision.amount,
            rail=settings.rail,
            status="scheduled",
            created_at=now,
            updated_at=now,
        )
        record_payment(tx, payment, now)
        made.append((payment, mandate.rail_token))
        span.set_attribute("payment_id", payment.payment_id)
        return 202, _out(payment)

    with telemetry.start_span("payment", {"tenant_id": tenant}) as span:
        with store.transaction(tenant) as tx:
            first = run_idempotent(tx, key, fingerprint, clock, write)
        if made:
            payment, rail_token = made[0]
        elif first.headers.get(REPLAYED) and (unanswered := _unanswered(store, tenant, first)):
            payment, rail_token = unanswered
        else:
            return first
        return _charge(store, clock, rail, tenant, payment, rail_token, key, fingerprint)


def _unanswered(
    store: StoreDep, tenant: TenantId, replay: Response
) -> tuple[Payment, RailToken] | None:
    """The Payment a replayed response is for, if the rail has not answered for it yet."""
    shown = PaymentOut.model_validate_json(bytes(replay.body))
    if shown.status != "scheduled" or shown.rail_payment_id is not None:
        return None
    with store.transaction(tenant) as tx:
        payment = tx.get_payment(shown.payment_id)
        mandate = tx.get_mandate(shown.mandate_id)
    if payment is None or mandate is None or mandate.rail_token is None:
        raise RuntimeError(f"Payment {shown.payment_id} lost its Mandate or Rail token")
    if payment.status != "scheduled" or payment.rail_payment_id is not None:
        return None  # answered since the response was kept
    return payment, mandate.rail_token


def _charge(
    store: StoreDep,
    clock: ClockDep,
    rail: PaymentRail,
    tenant: TenantId,
    payment: Payment,
    rail_token: RailToken,
    key: IdempotencyKey,
    fingerprint: str,
) -> Response:
    """Ask the rail, then record its answer, and keep that answer for replays of `key`."""
    try:
        charge = rail.schedule_charge(tenant, rail_token, payment.amount, payment.payment_id, key)
    except RailUnavailable:
        # Unclear: the charge may exist. The Payment stays scheduled; a retry or the
        # reconciliation sweep asks again by Payment id.
        log.warning("rail unavailable for Payment %s, Tenant %s", payment.payment_id, tenant)
        charge = None
    with store.transaction(tenant) as tx:
        current = apply_charge(tx, payment.payment_id, charge, decision_time(clock.now()))
        body = _out(current).model_dump_json().encode("utf-8")
        tx.save_idempotency_record(
            IdempotencyRecord(
                key=key,
                request_hash=fingerprint,
                status_code=202,
                body=body,
                created_at=payment.created_at,
            )
        )
    return Response(content=body, status_code=202, media_type="application/json")


@router.get("/{payment_id}")
def get_payment(payment_id: str, tenant: Tenant, store: StoreDep) -> PaymentOut:
    with store.transaction(tenant) as tx:
        payment = tx.get_payment(PaymentId(payment_id))
    if payment is None:
        raise ApiError(404, "PAYMENT_NOT_FOUND", f"No Payment {payment_id}.")
    return _out(payment)


_REFUSAL_TEXT = {
    "DECISION_NOT_PAYABLE": "Only an allowed Decision, or an approved step-up, can be paid.",
    "DECISION_ALREADY_PAID": "This Decision already has a Payment.",
    "PAYMENT_WINDOW_CLOSED": "The Payment window for this Decision has closed.",
    "MANDATE_REVOKED": "The Mandate was revoked.",
    "MANDATE_EXPIRED": "The Mandate has expired.",
    "MANDATE_NOT_YET_VALID": "The Mandate is not valid yet.",
    "AGENT_DISABLED": "The Agent is disabled.",
    "MANDATE_NOT_PAYABLE": "The Mandate has no Rail token this rail can charge.",
}
