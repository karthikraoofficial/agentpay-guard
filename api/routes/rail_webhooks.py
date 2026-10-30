"""/v1/rail/razorpay/webhooks: Razorpay reports how each Payment ended (#81).

No API key: Razorpay signs the raw body, and that signature is checked before the body is
read at all. A bad signature is a 400, logged without the body. A signed event we don't
act on (another type, or no Payment of ours) is a 200, so Razorpay stops retrying it.
Otherwise the Payment, and through it the Tenant, is found by our Payment id, and one
transaction applies the report through the shared transition and keeps the event id, so
a redelivery changes nothing.
"""

import logging

from fastapi import APIRouter, Header, Request, Response
from starlette.concurrency import run_in_threadpool

from adapters.razorpay.webhooks import payment_report, signature_ok
from api.deps import ClockDep, SettingsDep, StoreDep
from api.errors import ApiError
from core.charges import settle
from core.domain.decisions import decision_time
from core.domain.payments import RailEvent, RailReport
from core.ports import RailCharge

router = APIRouter(prefix="/v1/rail/razorpay", tags=["rail"])
log = logging.getLogger(__name__)


@router.post("/webhooks", status_code=200)
async def razorpay_webhook(
    request: Request,
    store: StoreDep,
    clock: ClockDep,
    settings: SettingsDep,
    signature: str | None = Header(default=None, alias="X-Razorpay-Signature"),
    event_id: str | None = Header(default=None, alias="X-Razorpay-Event-Id"),
) -> Response:
    raw = await request.body()
    secret = settings.razorpay_webhook_secret
    if not signature_ok(raw, signature, secret.get_secret_value() if secret else None):
        log.warning("rail webhook refused: bad or missing signature (event %s)", event_id)
        raise ApiError(400, "BAD_SIGNATURE", "The X-Razorpay-Signature does not match.")
    if not event_id:
        raise ApiError(400, "EVENT_ID_MISSING", "Send the X-Razorpay-Event-Id header.")
    report = payment_report(raw)
    if report is None:
        log.info("rail webhook %s dropped: not a payment event of ours", event_id)
        return Response(status_code=200)
    # The store is synchronous: keep it off the event loop.
    await run_in_threadpool(_apply, store, clock, event_id, report)
    return Response(status_code=200)


def _apply(store: StoreDep, clock: ClockDep, event_id: str, report: RailReport) -> None:
    tenant = store.payment_tenant(report.payment_id)
    if tenant is None:
        log.info("rail webhook %s dropped: no Payment %s", event_id, report.payment_id)
        return
    now = decision_time(clock.now())
    with store.transaction(tenant) as tx:
        payment = tx.get_payment(report.payment_id)
        if payment is None:
            return
        # The Mandate lock first, so two deliveries of one event can't both pass the check.
        tx.lock_mandate(payment.mandate_id)
        if tx.get_rail_event(event_id) is not None:
            return  # a redelivery
        if payment.amount != report.amount:
            log.warning(
                "rail webhook %s dropped: amount does not match Payment %s",
                event_id,
                report.payment_id,
            )
            return
        settle(
            tx,
            report.payment_id,
            RailCharge(
                payment_id=report.payment_id,
                status=report.status,
                amount=report.amount,
                rail_payment_id=report.rail_payment_id,
                failure_code=report.failure_code,
            ),
            now,
        )
        tx.add_rail_event(
            RailEvent(
                event_id=event_id,
                tenant_id=tenant,
                event_type=report.event_type,
                payment_id=report.payment_id,
                rail_payment_id=report.rail_payment_id,
                status=report.status,
                failure_code=report.failure_code,
                received_at=now,
            )
        )
