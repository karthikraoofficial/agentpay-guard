"""Demo controls (#280, ADR-0032): end one of the Demo Tenant's timers now, so a visitor
sees an expiry journey in seconds.

Mounted only when `AGENTPAY_DEMO_CONTROLS_TENANT_ID` is set, which settings allow only on
the mock rail. Each endpoint needs the `manage` scope and an idempotency key, and answers
404 for any Tenant but the Demo Tenant. A control never moves a clock or edits a Mandate,
Decision or Audit record: it writes its own `demo_controls` row and Audit record, and the
deadline it ends is read as "the real one, or the control's time if that came first".

- `POST /v1/demo/consent-requests/{id}/expire`: the request resolves `expired`, through the
  same transition the timer runs, with `resolved_via = demo_control`.
- `POST /v1/demo/decisions/{id}/expire-payment-window`: the Decision can no longer be paid
  (`PAYMENT_WINDOW_CLOSED`).
- `POST /v1/demo/payments/{id}/expire-notice`: the worker's debit step debits the Payment
  on its next run, or fails it if the Mandate no longer allows it.

The switches (#281) force a failure:

- `POST /v1/demo/mandates/{id}/ai-off`: model calls for this Mandate fail closed.
- `POST /v1/demo/mandates/{id}/rail-declining`: its charges and refunds answer failed.
- `POST /v1/demo/mandates/{id}/lose-payment`: its charges never reach the rail, so
  Reconciliation fails them with `RAIL_CHARGE_MISSING`.
- `POST /v1/demo/rail-down`: a simulated rail outage, with a cooldown.
- `GET /v1/demo/status`: public, whether an outage is running, for the banner on /demo.
- `GET /v1/demo/audit-records/{id}/tampered-copy`: verify an altered in-memory copy.
"""

import math
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel

from api.deps import BreakerDep, ClockDep, IdemKey, ManageTenant, SettingsDep, StoreDep
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from api.payability import was_granted
from core.audit_inspection import verify_tampered_copy
from core.demo_controls import expire_consent_now, record_control
from core.demo_switches import outage_ends_at
from core.domain.consent import ConsentRequestId
from core.domain.decisions import DecisionId, decision_time
from core.domain.demo_controls import RAIL_TARGET, DemoControl, DemoControlKind
from core.domain.mandates import MandateId
from core.domain.payments import PaymentId
from core.ports import TenantId
from core.settings import Settings

router = APIRouter(prefix="/v1/demo", tags=["demo controls"])


class DemoControlOut(BaseModel):
    control_id: str
    control: DemoControlKind
    target_id: str
    decision_id: DecisionId | None  # a switch runs before any Decision
    at: AwareDatetime
    audit_record_id: str


def _out(control: DemoControl) -> DemoControlOut:
    return DemoControlOut(
        control_id=control.control_id,
        control=control.kind,
        target_id=control.target_id,
        decision_id=control.decision_id,
        at=control.at,
        audit_record_id=control.audit_record_id,
    )


def _demo_only(tenant: TenantId, settings: Settings, what: str) -> None:
    """Any Tenant but the Demo Tenant sees nothing here, as if the target didn't exist."""
    if tenant != settings.demo_controls_tenant_id:
        raise ApiError(404, "NOT_FOUND", f"No {what}.")


@router.post("/consent-requests/{consent_request_id}/expire", response_model=DemoControlOut)
def expire_consent_request(
    request: Request,
    consent_request_id: str,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    settings: SettingsDep,
    key: IdemKey,
) -> Response:
    _demo_only(tenant, settings, f"Consent request {consent_request_id}")

    def write(read_at: datetime) -> tuple[int, DemoControlOut]:
        now = decision_time(read_at)
        consent = tx.lock_consent_request(ConsentRequestId(consent_request_id))
        if consent is None:
            raise ApiError(404, "NOT_FOUND", f"No Consent request {consent_request_id}.")
        if consent.resolution is not None:
            raise ApiError(
                409, "CONSENT_ALREADY_RESOLVED", "This Consent request has already resolved."
            )
        return 200, _out(expire_consent_now(tx, consent, now))

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, None), clock, write)


@router.post("/decisions/{decision_id}/expire-payment-window", response_model=DemoControlOut)
def expire_payment_window(
    request: Request,
    decision_id: str,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    settings: SettingsDep,
    key: IdemKey,
) -> Response:
    _demo_only(tenant, settings, f"Decision {decision_id}")

    def write(read_at: datetime) -> tuple[int, DemoControlOut]:
        now = decision_time(read_at)
        decision = tx.get_decision(DecisionId(decision_id))
        if decision is None:
            raise ApiError(404, "NOT_FOUND", f"No Decision {decision_id}.")
        if not was_granted(decision, tx.get_consent_request(decision.decision_id)):
            raise ApiError(409, "DECISION_NOT_PAYABLE", "This Decision never had a Payment window.")
        control = record_control(
            tx, "expire_payment_window", decision.decision_id, decision.decision_id, now
        )
        return 200, _out(control)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, None), clock, write)


@router.post("/payments/{payment_id}/expire-notice", response_model=DemoControlOut)
def expire_notice(
    request: Request,
    payment_id: str,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    settings: SettingsDep,
    key: IdemKey,
) -> Response:
    _demo_only(tenant, settings, f"Payment {payment_id}")

    def write(read_at: datetime) -> tuple[int, DemoControlOut]:
        now = decision_time(read_at)
        payment = tx.lock_payment(PaymentId(payment_id))
        if payment is None:
            raise ApiError(404, "NOT_FOUND", f"No Payment {payment_id}.")
        if payment.status != "scheduled" or payment.rail_payment_id is not None:
            raise ApiError(
                409, "NOTICE_NOT_RUNNING", "This Payment isn't waiting out a notice period."
            )
        control = record_control(tx, "expire_notice", payment.payment_id, payment.decision_id, now)
        return 200, _out(control)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, None), clock, write)


def _mandate_switch(
    request: Request,
    kind: DemoControlKind,
    mandate_id: str,
    tenant: TenantId,
    store: StoreDep,
    clock: ClockDep,
    settings: Settings,
    key: IdemKey,
) -> Response:
    """Turn a switch on for one Mandate. It stays on: each run grants its own Mandate."""
    _demo_only(tenant, settings, f"Mandate {mandate_id}")

    def write(read_at: datetime) -> tuple[int, DemoControlOut]:
        now = decision_time(read_at)
        mandate = tx.lock_mandate(MandateId(mandate_id))
        if mandate is None:
            raise ApiError(404, "NOT_FOUND", f"No Mandate {mandate_id}.")
        existing = tx.get_demo_control(kind, mandate.mandate_id)
        if existing is not None:
            return 200, _out(existing)
        return 200, _out(record_control(tx, kind, mandate.mandate_id, None, now))

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, None), clock, write)


@router.post("/mandates/{mandate_id}/ai-off", response_model=DemoControlOut)
def ai_off(
    request: Request,
    mandate_id: str,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    settings: SettingsDep,
    key: IdemKey,
) -> Response:
    return _mandate_switch(request, "ai_off", mandate_id, tenant, store, clock, settings, key)


@router.post("/mandates/{mandate_id}/rail-declining", response_model=DemoControlOut)
def rail_declining(
    request: Request,
    mandate_id: str,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    settings: SettingsDep,
    key: IdemKey,
) -> Response:
    return _mandate_switch(
        request, "rail_declining", mandate_id, tenant, store, clock, settings, key
    )


@router.post("/mandates/{mandate_id}/lose-payment", response_model=DemoControlOut)
def lose_payment(
    request: Request,
    mandate_id: str,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    settings: SettingsDep,
    key: IdemKey,
) -> Response:
    return _mandate_switch(request, "lose_payment", mandate_id, tenant, store, clock, settings, key)


@router.post("/rail-down", response_model=DemoControlOut)
def rail_down(
    request: Request,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    settings: SettingsDep,
    breaker: BreakerDep,
    key: IdemKey,
) -> Response:
    """Start a simulated rail outage, or join the one running. It affects every visitor, so
    another can't start until the cooldown from this one's start is over."""
    _demo_only(tenant, settings, "rail outage")
    outage = timedelta(seconds=settings.demo_rail_outage_seconds)
    cooldown = timedelta(seconds=settings.demo_rail_cooldown_seconds)
    running: list[bool] = []

    def write(read_at: datetime) -> tuple[int, DemoControlOut]:
        now = decision_time(read_at)
        tx.lock_chain_head()  # one writer at a time, so two visitors can't start two outages
        latest = tx.latest_demo_control("rail_down", RAIL_TARGET)
        if latest is not None and now < latest.at + outage:
            running.append(True)
            return 200, _out(latest)
        if latest is not None and now < latest.at + cooldown:
            wait = math.ceil((latest.at + cooldown - now).total_seconds())
            raise ApiError(
                409,
                "RAIL_DOWN_COOLDOWN",
                "A simulated rail outage ran recently. Try again after Retry-After seconds.",
                headers={"Retry-After": str(wait)},
            )
        running.append(True)
        return 200, _out(record_control(tx, "rail_down", RAIL_TARGET, None, now))

    with store.transaction(tenant) as tx:
        answer = run_idempotent(tx, key, request_hash(request, None), clock, write)
    if running:  # a replayed answer doesn't open the breaker again
        breaker.trip(clock.now())
    return answer


class DemoStatus(BaseModel):
    rail_outage: bool
    outage_ends_at: AwareDatetime | None


@router.get("/status", response_model=DemoStatus)
def demo_status(store: StoreDep, clock: ClockDep, settings: SettingsDep) -> DemoStatus:
    """Public, for the banner on /demo: whether a simulated rail outage is running. It says
    nothing about any Tenant's data."""
    tenant = TenantId(str(settings.demo_controls_tenant_id))
    outage = timedelta(seconds=settings.demo_rail_outage_seconds)
    with store.transaction(tenant) as tx:
        ends = outage_ends_at(tx, outage, clock.now())
    return DemoStatus(rail_outage=ends is not None, outage_ends_at=ends)


class TamperedCopyOut(BaseModel):
    clean: bool
    record_count: int
    first_broken_seq: int | None
    tampered_seq: int


@router.get("/audit-records/{audit_record_id}/tampered-copy", response_model=TamperedCopyOut)
def tampered_copy(
    audit_record_id: str, tenant: ManageTenant, store: StoreDep, settings: SettingsDep
) -> TamperedCopyOut:
    """Alter this record in an in-memory copy of the chain and verify the copy. The
    database is never changed."""
    _demo_only(tenant, settings, f"Audit record {audit_record_id}")
    with store.transaction(tenant) as tx:
        found = verify_tampered_copy(tx, audit_record_id)
    if found is None:
        raise ApiError(404, "NOT_FOUND", f"No Audit record {audit_record_id}.")
    report, seq = found
    return TamperedCopyOut(
        clean=report.clean,
        record_count=report.record_count,
        first_broken_seq=report.first_broken_seq,
        tampered_seq=seq,
    )
