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

A Tenant refunds a captured Payment the same way (PRD #205, ADR-0024): a pending Refund
and its `refund` Audit record first, then the rail, keyed by the Refund id, then its
answer. Only a `manage` key may refund: an Agent never moves money back.
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from api.deps import (
    BreakerDep,
    ClockDep,
    DecideTenant,
    IdemKey,
    ManageTenant,
    PackDep,
    RailDep,
    SettingsDep,
    StoreDep,
    TelemetryDep,
    Tenant,
)
from api.errors import ApiError
from api.idempotency import REPLAYED, request_hash, run_idempotent
from api.payability import demo_tenant, payable_until
from core.charges import apply_charge, record_payment
from core.domain.decisions import DecisionId, PackRef, decision_time
from core.domain.idempotency import IdempotencyKey, IdempotencyRecord
from core.domain.mandates import MandateId
from core.domain.money import Money
from core.domain.parties import new_id
from core.domain.payments import (
    Payment,
    PaymentCreate,
    PaymentId,
    PaymentStatus,
    payment_refusal,
)
from core.domain.rails import RailName, RailToken
from core.domain.refunds import (
    REASON_MAX,
    Refund,
    RefundId,
    RefundStatus,
    refund_refusal,
)
from core.ports import PaymentRail, RailUnavailable, TenantId
from core.rail_breaker import RailBreaker
from core.refunds import record_refund, request_refund

router = APIRouter(prefix="/v1/payments", tags=["payments"])
log = logging.getLogger(__name__)


class RefundBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Money
    # The Tenant's own words, if any. Only its hash goes into the Audit chain.
    reason: str | None = Field(default=None, min_length=1, max_length=REASON_MAX)

    @field_validator("amount")
    @classmethod
    def _above_zero(cls, amount: Money) -> Money:
        if amount.minor <= 0:
            raise ValueError("a refund is for more than zero")
        return amount


class RefundOut(BaseModel):
    refund_id: RefundId
    payment_id: PaymentId
    amount: Money
    reason: str | None
    status: RefundStatus
    rail_refund_id: str | None
    failure_code: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class PaymentOut(BaseModel):
    payment_id: PaymentId
    decision_id: DecisionId
    mandate_id: MandateId
    amount: Money
    rail: RailName
    status: PaymentStatus
    rail_payment_id: str | None
    failure_code: str | None
    # When the card will be debited, after the Pre-debit notice (ADR-0017). Absent from
    # replays of Payments made before it existed.
    debit_after: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    # Its Refunds, oldest first, and the total of the processed ones. Absent from replays
    # of Payments made before refunds existed.
    refunds: list[RefundOut] = []
    refunded: Money | None = None


def _out(payment: Payment, refunds: list[Refund] | None = None) -> PaymentOut:
    shown = refunds or []
    refunded = sum(r.amount.minor for r in shown if r.status == "processed")
    return PaymentOut.model_validate(
        payment.model_dump(include=set(PaymentOut.model_fields) - {"refunds", "refunded"})
        | {
            "refunds": [_refund_out(r) for r in shown],
            "refunded": Money(minor=refunded, currency=payment.amount.currency),
        }
    )


def _refund_out(refund: Refund) -> RefundOut:
    return RefundOut.model_validate(refund.model_dump(include=set(RefundOut.model_fields)))


@router.post("", status_code=202, response_model=PaymentOut)
def create_payment(
    request: Request,
    body: PaymentCreate,
    tenant: DecideTenant,
    store: StoreDep,
    clock: ClockDep,
    pack: PackDep,
    settings: SettingsDep,
    rail: RailDep,
    breaker: BreakerDep,
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
            payable_until=payable_until(
                tx,
                decision,
                consent,
                pack,
                settings.policy_packs_dir,
                demo_tenant=demo_tenant(settings),
            ),
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
        _fail_fast(breaker, clock)  # last, so only a request that would reach the rail tries
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
        elif (
            first.headers.get(REPLAYED)
            and (unanswered := _unanswered(store, tenant, first, clock, settings, rail))
            and breaker.refusal(clock.now()) is None
        ):
            payment, rail_token = unanswered
        else:
            return first
        return _charge(store, clock, rail, breaker, tenant, payment, rail_token, key, fingerprint)


def _unanswered(
    store: StoreDep,
    tenant: TenantId,
    replay: Response,
    clock: ClockDep,
    settings: SettingsDep,
    rail: PaymentRail,
) -> tuple[Payment, RailToken] | None:
    """The Payment a replayed response is for, if the rail has not answered for it yet and
    a retry may still ask. Not before the rail's lookup lag: until then the rail might not
    find the charge it made and make a second. And only in the first half of the
    reconciliation age: after that the sweep owns the Payment, and may fail one the rail
    never saw, so a retry asking the rail to charge then could charge a Payment the sweep
    is failing (#82)."""
    shown = PaymentOut.model_validate_json(bytes(replay.body))
    if shown.status != "scheduled" or shown.rail_payment_id or shown.debit_after:
        return None
    with store.transaction(tenant) as tx:
        payment = tx.get_payment(shown.payment_id)
        mandate = tx.get_mandate(shown.mandate_id)
    if payment is None or mandate is None or mandate.rail_token is None:
        raise RuntimeError(f"Payment {shown.payment_id} lost its Mandate or Rail token")
    if payment.status != "scheduled" or payment.rail_answered:
        return None  # answered since the response was kept
    retry_until = payment.created_at + timedelta(seconds=settings.reconcile_after_seconds / 2)
    if not payment.created_at + rail.lookup_lag <= clock.now() < retry_until:
        return None
    return payment, mandate.rail_token


def _fail_fast(breaker: RailBreaker, clock: ClockDep) -> None:
    """Refuse with 503 RAIL_UNAVAILABLE while the breaker is open. Raised inside the write,
    so nothing is written and the Idempotency-Key stays free for the retry."""
    wait = breaker.refusal(clock.now())
    if wait is not None:
        raise ApiError(
            503,
            "RAIL_UNAVAILABLE",
            "The rail isn't answering. Nothing was written: retry after Retry-After seconds.",
            headers={"Retry-After": str(wait)},
        )


def _heard(breaker: RailBreaker, clock: ClockDep, unknown: bool) -> None:
    """Tell the breaker how a rail call went: an answer, or an unknown outcome."""
    if not unknown:
        if breaker.answered():
            log.info("rail breaker closed: the rail answered")
    elif breaker.unavailable(clock.now()):
        log.warning("rail breaker opened: payments and refunds answer 503 RAIL_UNAVAILABLE")


def _charge(
    store: StoreDep,
    clock: ClockDep,
    rail: PaymentRail,
    breaker: RailBreaker,
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
    _heard(breaker, clock, unknown=charge is None)
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
        refunds = [] if payment is None else tx.list_refunds(payment.payment_id)
    if payment is None:
        raise ApiError(404, "PAYMENT_NOT_FOUND", f"No Payment {payment_id}.")
    return _out(payment, refunds)


@router.get("/{payment_id}/refunds/{refund_id}")
def get_refund(payment_id: str, refund_id: str, tenant: Tenant, store: StoreDep) -> RefundOut:
    with store.transaction(tenant) as tx:
        found = [r for r in tx.list_refunds(PaymentId(payment_id)) if r.refund_id == refund_id]
    if not found:
        raise ApiError(404, "REFUND_NOT_FOUND", f"No Refund {refund_id} on Payment {payment_id}.")
    return _refund_out(found[0])


@router.post("/{payment_id}/refunds", status_code=201, response_model=RefundOut)
def create_refund(
    request: Request,
    payment_id: str,
    body: RefundBody,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    pack: PackDep,
    rail: RailDep,
    breaker: BreakerDep,
    key: IdemKey,
) -> Response:
    fingerprint = request_hash(request, body)
    made: list[Refund] = []

    def write(read_at: datetime) -> tuple[int, RefundOut]:
        now = decision_time(read_at)
        found = tx.get_payment(PaymentId(payment_id))
        if found is None:
            raise ApiError(404, "PAYMENT_NOT_FOUND", f"No Payment {payment_id}.")
        if body.amount.currency != found.amount.currency:
            raise ApiError(
                422,
                "VALIDATION_FAILED",
                f"amount must be in the Payment's currency, {found.amount.currency}.",
            )
        # Mandate before Payment, as rail reports and Disputes take them, so all queue.
        tx.lock_mandate(found.mandate_id)
        payment = tx.lock_payment(found.payment_id)
        if payment is None:
            raise RuntimeError(f"Payment {payment_id} vanished")
        dispute = tx.get_dispute_for_payment(payment.payment_id)
        refusal = refund_refusal(
            payment,
            None if dispute is None else dispute.status,
            tx.list_refunds(payment.payment_id),
            body.amount,
            pack.pack.refund_window,
            now,
        )
        if refusal is not None:
            log.info("refund refused %s: Payment %s, Tenant %s", refusal, payment_id, tenant)
            raise ApiError(409, refusal, _REFUND_REFUSAL_TEXT[refusal])
        _fail_fast(breaker, clock)  # last, so only a request that would reach the rail tries
        ref = PackRef(id=pack.pack.id, version=pack.pack.version, hash=pack.hash)
        refund = request_refund(tx, payment, body.amount, body.reason, ref, now)
        made.append(refund)
        return 201, _refund_out(refund)

    with store.transaction(tenant) as tx:
        first = run_idempotent(tx, key, fingerprint, clock, write)
    if not made:
        return first
    refund = made[0]
    try:
        answer = rail.refund(tenant, refund.payment_id, refund.refund_id, refund.amount, key)
    except RailUnavailable:
        # Unclear: the rail may have refunded. The Refund stays pending, to be asked about
        # again by its id (#209).
        log.warning("rail unavailable for Refund %s, Tenant %s", refund.refund_id, tenant)
        answer = None
    _heard(breaker, clock, unknown=answer is None)
    with store.transaction(tenant) as tx:
        current = record_refund(tx, refund.refund_id, answer, decision_time(clock.now()))
        content = _refund_out(current).model_dump_json().encode("utf-8")
        tx.save_idempotency_record(
            IdempotencyRecord(
                key=key,
                request_hash=fingerprint,
                status_code=201,
                body=content,
                created_at=refund.created_at,
            )
        )
    return Response(content=content, status_code=201, media_type="application/json")


_REFUND_REFUSAL_TEXT = {
    "PAYMENT_NOT_REFUNDABLE": "Only a captured Payment can be refunded; cancel a scheduled one.",
    "PAYMENT_DISPUTED": "This Payment has a Dispute; a refund now could pay the Payer twice.",
    "REFUND_WINDOW_CLOSED": "The Refund window for this Payment has closed.",
    "REFUND_EXCEEDS_CAPTURED": "That would refund more than was captured on this Payment.",
}

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
