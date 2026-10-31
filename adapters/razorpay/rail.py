"""RazorpayRail: the PaymentRail on Razorpay card recurring payments, in test mode
(ADR-0012, ADR-0013).

A card debit is two steps, 36 h 5 min apart (ADR-0017). `schedule_charge` is the first:
it finds or makes the Razorpay order for a Payment, with our Payment id as the order's
`receipt` and in its notes, and a Pre-debit notice for the Rail token. Until a debit is
made on the order, the charge is `scheduled`, with `debit_after` set to when the notice
period ends.

One order per Payment (ADR-0014): Razorpay orders take no idempotency key, so the order is
looked up by receipt before one is made, and a retry after a lost answer finds it.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from adapters.razorpay.api import RazorpayApi, RazorpayRefused
from adapters.razorpay.webhooks import PAYMENT_ID_NOTE
from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
from core.domain.rails import RailToken
from core.ports import IdempotencyKey, PaymentRail, RailCharge, RailUnavailable
from core.settings import Settings

log = logging.getLogger(__name__)

# The notes key that ties an order to its Tenant: one rail account serves every Tenant,
# so a Tenant only ever sees orders carrying its own id (ADR-0012).
TENANT_NOTE = "agentpay_tenant_id"
# A refunded payment was captured first; refunds arrive with disputes in Phase 5.
_CAPTURED = frozenset({"captured", "refunded"})


class RazorpayRail:
    # Razorpay's order search found a new order by its receipt after about 25 s (#83);
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
            return self._charge(payment_id, existing)
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

    def get_charge(self, tenant_id: TenantId, payment_id: PaymentId) -> RailCharge | None:
        order = self._find_order(tenant_id, payment_id)
        return None if order is None else self._charge(payment_id, order)

    def refund(
        self,
        tenant_id: TenantId,
        payment_id: PaymentId,
        amount: Money,
        idempotency_key: IdempotencyKey,
    ) -> RailCharge:
        raise NotImplementedError("Refunds come with disputes in Phase 5")

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

    def _charge(self, payment_id: PaymentId, order: dict[str, Any]) -> RailCharge:
        payments = self._lookup(f"/orders/{order['id']}/payments")
        settled = _parsed(lambda: _settled(payments["items"]))
        if settled is None:  # no debit yet, or one still in flight
            full = self._lookup(f"/orders/{order['id']}")  # the receipt lookup omits the notice
            return _parsed(lambda: _scheduled(payment_id, full))
        return _parsed(lambda: _debited(payment_id, settled))

    def _lookup(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """A read whose refusal says nothing about the charge, so its outcome is unknown."""
        try:
            return self._api.get(path, params)
        except RazorpayRefused as refused:
            raise RailUnavailable(f"Razorpay refused a lookup: {refused}") from None


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


def _debited(payment_id: PaymentId, payment: dict[str, Any]) -> RailCharge:
    captured = payment["status"] in _CAPTURED
    return RailCharge(
        payment_id=payment_id,
        status="captured" if captured else "failed",
        amount=Money(minor=payment["amount"], currency=payment["currency"]),
        rail_payment_id=payment["id"],
        failure_code=None if captured else (payment.get("error_code") or "UNKNOWN"),
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
