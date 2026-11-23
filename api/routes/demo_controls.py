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
"""

from datetime import datetime

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel

from api.deps import ClockDep, IdemKey, ManageTenant, SettingsDep, StoreDep
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from api.payability import was_granted
from core.demo_controls import expire_consent_now, record_control
from core.domain.consent import ConsentRequestId
from core.domain.decisions import DecisionId, decision_time
from core.domain.demo_controls import DemoControl, DemoControlKind
from core.domain.payments import PaymentId
from core.ports import TenantId
from core.settings import Settings

router = APIRouter(prefix="/v1/demo", tags=["demo controls"])


class DemoControlOut(BaseModel):
    control_id: str
    control: DemoControlKind
    target_id: str
    decision_id: DecisionId
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
