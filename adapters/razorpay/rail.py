"""RazorpayRail: the PaymentRail on Razorpay card recurring payments, in test mode
(ADR-0012, ADR-0013).

A card debit is two steps, 36 h 5 min apart (ADR-0017). `schedule_charge` is the first:
it finds or makes the Razorpay order for a Payment, with our Payment id as the order's
`receipt` and in its notes, and a Pre-debit notice for the Rail token. Until a debit is
made on the order, the charge is `scheduled`, with `debit_after` set to when the notice
period ends, or `failed` with `RAIL_NOTICE_FAILED` if Razorpay marked the notice failed.
`debit` is the second: the recurring payment on that order, never tried after a failed
notice.

One order per Payment (ADR-0014): Razorpay orders take no idempotency key, so the order is
looked up by receipt before one is made, and a retry after a lost answer finds it.

Refunds (#210, ADR-0024) work the same way: a refund is made on the Payment's captured
Razorpay payment with our Refund id as its `receipt` and in its notes, and that payment's
refunds are read first, so asking again for a Refund finds the one already made. The list
is fetched by payment id, not searched, so a new refund is in it at once.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from adapters.razorpay.api import RazorpayApi, RazorpayRefused
from adapters.razorpay.webhooks import PAYMENT_ID_NOTE, REFUND_ID_NOTE, TENANT_NOTE
from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
from core.domain.rails import RAIL_NOTICE_FAILED, RailToken
from core.domain.refunds import RefundId, RefundStatus
from core.ports import IdempotencyKey, PaymentRail, RailCharge, RailRefund, RailUnavailable
from core.settings import Settings

log = logging.getLogger(__name__)

# A refunded payment was captured first.
_CAPTURED = frozenset({"captured", "refunded"})
# The failure code of a Refund whose Payment Razorpay never captured.
PAYMENT_NOT_CAPTURED = "PAYMENT_NOT_CAPTURED"
_REFUND_STATUSES: dict[str, RefundStatus] = {
    "created": "pending",
    "pending": "pending",
    "processed": "processed",
    "failed": "failed",
}


class RazorpayRail:
    # Razorpay's order search found a new order by its receipt after about 25 s on the
    # test account (ADR-0014);
    # fetching an order by id is immediate, but a lost answer leaves us no id.
    lookup_lag = timedelta(minutes=2)

    def __init__(
        self,
        key_id: str,
        key_secret: SecretStr,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api = RazorpayApi(key_id, key_secret, transport)

    @classmethod
    def from_settings(cls, settings: Settings) -> "RazorpayRail":
        if settings.razorpay_key_id is None or settings.razorpay_key_secret is None:
            raise ValueError("AGENTPAY_RAZORPAY_KEY_ID and AGENTPAY_RAZORPAY_KEY_SECRET are needed")
        return cls(settings.razorpay_key_id, settings.razorpay_key_secret)

    def schedule_charge(
        self,
        tenant_id: TenantId,
        rail_token: RailToken,
        amount: Money,
        payment_id: PaymentId,
        idempotency_key: IdempotencyKey,
    ) -> RailCharge:
        existing = self._find_order(tenant_id, payment_id)
        if existing is not None:
            charge = self._charge(payment_id, existing)
            if charge.amount != amount:  # a Payment is always its Decision's amount (ADR-0014)
                raise ValueError(f"Razorpay's order for Payment {payment_id} is for another amount")
            return charge
        try:
            order = self._api.post(
                "/orders",
                {
                    "amount": amount.minor,
                    "currency": amount.currency,
                    "payment_capture": True,
                    "receipt": payment_id,
                    "notes": {PAYMENT_ID_NOTE: payment_id, TENANT_NOTE: tenant_id},
                    "notification": {"token_id": rail_token.token_ref},
                },
            )
        except RazorpayRefused as refused:
            log.warning("Razorpay refused the order for Payment %s: %s", payment_id, refused)
            return RailCharge(
                payment_id=payment_id, status="failed", amount=amount, failure_code=refused.code
            )
        return _parsed(lambda: _scheduled(payment_id, order))

    def debit(
        self,
        tenant_id: TenantId,
        rail_token: RailToken,
        amount: Money,
        payment_id: PaymentId,
        idempotency_key: IdempotencyKey,
    ) -> RailCharge | None:
        found = self._find_order(tenant_id, payment_id)
        if found is None:
            return None
        # By id, not the receipt search: this read is never behind a debit just made.
        order = self._lookup(f"/orders/{found['id']}")
        payments = self._lookup(f"/orders/{found['id']}/payments")
        if payments["items"] or order.get("attempts") or order.get("status") != "created":
            return self._charge(payment_id, order, payments)  # debited already: never twice
        undebited = _parsed(lambda: _undebited(payment_id, order))
        if undebited.status == "failed":
            return undebited  # the notice failed: Razorpay would refuse every debit
        customer = self._lookup(f"/customers/{rail_token.customer_ref}")
        try:
            made = self._api.post(
                "/payments/create/recurring",
                {
                    # Razorpay requires the Payer's email and contact on every debit; they
                    # are read from its own customer record, passed on and never kept.
                    "email": customer["email"],
                    "contact": customer["contact"],
                    "amount": amount.minor,
                    "currency": amount.currency,
                    "order_id": order["id"],
                    "customer_id": rail_token.customer_ref,
                    "token": rail_token.token_ref,
                    "recurring": "1",
                    "notes": {PAYMENT_ID_NOTE: payment_id, TENANT_NOTE: tenant_id},
                },
            )
        except RazorpayRefused as refused:
            if _not_yet(refused):
                return _parsed(lambda: _scheduled(payment_id, order))
            log.warning("Razorpay refused the debit for Payment %s: %s", payment_id, refused)
            return RailCharge(
                payment_id=payment_id, status="failed", amount=amount, failure_code=refused.code
            )
        scheduled = _parsed(lambda: _scheduled(payment_id, order))
        return scheduled.model_copy(
            update={"rail_payment_id": _parsed(lambda: str(made["razorpay_payment_id"]))}
        )

    def get_charge(self, tenant_id: TenantId, payment_id: PaymentId) -> RailCharge | None:
        order = self._find_order(tenant_id, payment_id)
        return None if order is None else self._charge(payment_id, order)

    def refund(
        self,
        tenant_id: TenantId,
        payment_id: PaymentId,
        refund_id: RefundId,
        amount: Money,
        idempotency_key: IdempotencyKey,
    ) -> RailRefund:
        paid = self._captured_payment(tenant_id, payment_id)
        if paid is None:
            return RailRefund(
                refund_id=refund_id,
                status="failed",
                amount=amount,
                failure_code=PAYMENT_NOT_CAPTURED,
            )
        found = self._find_refund(tenant_id, paid, refund_id)
        if found is not None:
            return found
        try:
            made = self._api.post(
                f"/payments/{paid}/refund",
                {
                    "amount": amount.minor,
                    "speed": "normal",
                    "receipt": refund_id,
                    "notes": {
                        REFUND_ID_NOTE: refund_id,
                        PAYMENT_ID_NOTE: payment_id,
                        TENANT_NOTE: tenant_id,
                    },
                },
            )
        except RazorpayRefused as refused:
            return RailRefund(
                refund_id=refund_id, status="failed", amount=amount, failure_code=refused.code
            )
        return _parsed(lambda: _refunded(refund_id, made))

    def get_refund(
        self, tenant_id: TenantId, payment_id: PaymentId, refund_id: RefundId
    ) -> RailRefund | None:
        paid = self._captured_payment(tenant_id, payment_id)
        return None if paid is None else self._find_refund(tenant_id, paid, refund_id)

    def token_expires_at(self, rail_token: RailToken) -> datetime | None:
        """When Razorpay expires the token, or None if it doesn't know the token. Test
        tokens last until 23:59:59 IST the day after registration."""
        try:
            token = self._api.get(
                f"/customers/{rail_token.customer_ref}/tokens/{rail_token.token_ref}"
            )
        except RazorpayRefused:
            return None
        return _parsed(lambda: _time(token["expired_at"]))

    def orders_for(self, payment_id: PaymentId) -> int:
        """Test hook: how many Razorpay orders carry this Payment id as their receipt."""
        return len(self._lookup("/orders", {"receipt": payment_id})["items"])

    # --- reading Razorpay ----------------------------------------------------------------

    def _find_order(self, tenant_id: TenantId, payment_id: PaymentId) -> dict[str, Any] | None:
        found = self._lookup("/orders", {"receipt": payment_id})
        ours: list[dict[str, Any]] = _parsed(
            lambda: [
                o for o in found["items"] if (o.get("notes") or {}).get(TENANT_NOTE) == tenant_id
            ]
        )
        if len(ours) > 1:
            log.error(
                "Razorpay has %d orders for Payment %s; using the first", len(ours), payment_id
            )
        return min(ours, key=lambda o: int(o["created_at"])) if ours else None

    def _charge(
        self,
        payment_id: PaymentId,
        order: dict[str, Any],
        payments: dict[str, Any] | None = None,
    ) -> RailCharge:
        if payments is None:
            payments = self._lookup(f"/orders/{order['id']}/payments")
        items = payments["items"]
        settled = _parsed(lambda: _settled(items))
        if settled is not None:
            return _parsed(lambda: _debited(payment_id, settled))
        if "notification" not in order:  # the receipt search leaves the notice out
            order = self._lookup(f"/orders/{order['id']}")
        # A debit made and still in flight.
        made = _parsed(lambda: max(items, key=lambda p: int(p["created_at"]), default=None))
        if made is None and not order.get("attempts") and order.get("status") == "created":
            return _parsed(lambda: _undebited(payment_id, order))
        # Attempted, but the payment isn't listed yet: still in flight, whatever the notice.
        scheduled = _parsed(lambda: _scheduled(payment_id, order))
        return scheduled.model_copy(update={"rail_payment_id": made["id"]}) if made else scheduled

    def _captured_payment(self, tenant_id: TenantId, payment_id: PaymentId) -> str | None:
        """Razorpay's id for the Payment's captured payment, or None if it has none."""
        order = self._find_order(tenant_id, payment_id)
        if order is None:
            return None
        charge = self._charge(payment_id, order)
        return charge.rail_payment_id if charge.status == "captured" else None

    def _find_refund(
        self, tenant_id: TenantId, paid: str, refund_id: RefundId
    ) -> RailRefund | None:
        found = self._lookup(f"/payments/{paid}/refunds")
        ours: list[dict[str, Any]] = _parsed(
            lambda: [
                r
                for r in found["items"]
                if (r.get("notes") or {}).get(REFUND_ID_NOTE) == refund_id
                and (r.get("notes") or {}).get(TENANT_NOTE) == tenant_id
            ]
        )
        return _parsed(lambda: _refunded(refund_id, ours[0])) if ours else None

    def _lookup(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """A read whose refusal says nothing about the charge, so its outcome is unknown."""
        try:
            return self._api.get(path, params)
        except RazorpayRefused as refused:
            raise RailUnavailable(f"Razorpay refused a lookup: {refused}") from None


def _not_yet(refused: RazorpayRefused) -> bool:
    """Whether Razorpay refused a debit only because the notice period isn't over: before
    the notice is delivered, and after it until 36 h 5 min have passed (ADR-0017). A notice
    that failed gives the same refusal, but `debit` reads the order first and stops there."""
    if refused.reason == "pre_debit_notification_failed":
        return True
    return refused.reason == "input_validation_failed" and (
        "debit can be attempted" in refused.description.lower()
    )


def _settled(payments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The payment that decides the charge: a captured one if any, else the latest if it
    failed. A created or authorized payment is still in flight."""
    captured = [p for p in payments if p["status"] in _CAPTURED]
    if captured:
        return captured[0]
    latest = max(payments, key=lambda p: int(p["created_at"]), default=None)
    return latest if latest is not None and latest["status"] == "failed" else None


def _scheduled(payment_id: PaymentId, order: dict[str, Any]) -> RailCharge:
    notice = order.get("notification") or {}
    after = notice.get("payment_after")
    return RailCharge(
        payment_id=payment_id,
        status="scheduled",
        amount=Money(minor=order["amount"], currency=order["currency"]),
        debit_after=_time(after) if after is not None else None,
    )


def _undebited(payment_id: PaymentId, order: dict[str, Any]) -> RailCharge:
    """A charge with no debit made: scheduled, or failed if Razorpay marked its Pre-debit
    notice failed, after which it refuses every debit on the order."""
    scheduled = _scheduled(payment_id, order)
    if (order.get("notification") or {}).get("status") != "failed":
        return scheduled
    return scheduled.model_copy(update={"status": "failed", "failure_code": RAIL_NOTICE_FAILED})


def _debited(payment_id: PaymentId, payment: dict[str, Any]) -> RailCharge:
    captured = payment["status"] in _CAPTURED
    return RailCharge(
        payment_id=payment_id,
        status="captured" if captured else "failed",
        amount=Money(minor=payment["amount"], currency=payment["currency"]),
        rail_payment_id=payment["id"],
        failure_code=None if captured else (payment.get("error_code") or "UNKNOWN"),
    )


def _refunded(refund_id: RefundId, refund: dict[str, Any]) -> RailRefund:
    status = _REFUND_STATUSES[refund["status"]]
    return RailRefund(
        refund_id=refund_id,
        status=status,
        amount=Money(minor=refund["amount"], currency=refund["currency"]),
        rail_refund_id=refund["id"],
        failure_code="REFUND_FAILED" if status == "failed" else None,
    )


def _time(epoch: Any) -> datetime:
    return datetime.fromtimestamp(int(epoch), UTC)


def _parsed[T](read: Callable[[], T]) -> T:
    """Run a read of Razorpay's body. A body that isn't the shape we know leaves the outcome
    unknown, never a guess."""
    try:
        return read()
    except (KeyError, TypeError, ValueError) as exc:
        raise RailUnavailable(f"Razorpay answered in an unexpected shape: {exc!r}") from None


_conforms: type[PaymentRail] = RazorpayRail  # mypy checks the adapter matches the port
