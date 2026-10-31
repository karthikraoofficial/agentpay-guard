"""A fake Razorpay API behind an httpx transport, shaped by what the real test account
returned when probed:

- `GET /orders?receipt=` lists orders without their `notification`; `GET /orders/{id}`
  includes it.
- An order's notification sets `payment_after` to 36 h 5 min after the order (ADR-0017).
- A notification for a token Razorpay doesn't know is a 400 `BAD_REQUEST_ERROR`.
- A recurring payment before the notice is delivered fails `pre_debit_notification_failed`;
  after delivery but before `payment_after`, `input_validation_failed` ("Debit can be
  attempted 36 hours after..."). Neither creates a payment.
"""

import json
import threading
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

NOTICE_SECONDS = 36 * 3600 + 5 * 60
CREATED_AT = 1_793_391_196  # 2026-10-30T20:13:16Z, the prototype's notice order


@dataclass
class FakeRazorpay:
    EMAIL: ClassVar[str] = "payer@example.com"
    CONTACT: ClassVar[str] = "9000090000"

    tokens: set[str] = field(default_factory=set)
    orders: list[dict[str, Any]] = field(default_factory=list)
    payments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    token_expiry: dict[str, int] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    # Knobs: the next call answers with this status, or times out (optionally after
    # the order was made, so the caller never hears that it exists).
    next_status: int | None = None
    next_times_out: bool = False
    times_out_after_creating: bool = False
    # The fake's clock (epoch seconds), whether notices get delivered, and whether every
    # recurring payment is refused outright.
    now: int = CREATED_AT
    notice_delivered: bool = True
    refuse_debits: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def settle(self, receipt: str, status: str = "captured", error_code: str | None = None) -> str:
        """Razorpay finishes the debit on the order with the receipt (making one if none
        was made); returns the payment's id."""
        order = next(o for o in self.orders if o["receipt"] == receipt)
        items = self.payments.setdefault(order["id"], [])
        pending = [p for p in items if p["status"] == "created"]
        if pending:
            pending[0].update(status=status, error_code=error_code)
            return str(pending[0]["id"])
        payment = self._payment(order, order["notes"])
        payment.update(status=status, error_code=error_code, created_at=self.now + 200_000)
        return str(payment["id"])

    def orders_with(self, receipt: str) -> int:
        return sum(1 for o in self.orders if o["receipt"] == receipt)

    # --- the API -------------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
            if self.next_times_out:
                self.next_times_out = False
                raise httpx.ReadTimeout("fake timeout", request=request)
            if self.next_status is not None:
                status, self.next_status = self.next_status, None
                return _error(status, "SERVER_ERROR", "NA")
            return self._route(request)

    def _route(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.removeprefix("/v1/").split("/")
        if request.method == "POST" and parts == ["orders"]:
            return self._create_order(json.loads(request.content))
        if request.method == "POST" and parts == ["payments", "create", "recurring"]:
            return self._debit(json.loads(request.content))
        if request.method == "GET" and parts == ["orders"]:
            receipt = request.url.params.get("receipt")
            return _collection([_listed(o) for o in self.orders if o["receipt"] == receipt])
        if request.method == "GET" and len(parts) == 2 and parts[0] == "orders":
            order = next((o for o in self.orders if o["id"] == parts[1]), None)
            return (
                httpx.Response(200, json=order) if order else _error(400, "BAD_REQUEST_ERROR", "NA")
            )
        if request.method == "GET" and len(parts) == 3 and parts[2] == "payments":
            return _collection(self.payments.get(parts[1], []))
        if request.method == "GET" and len(parts) == 2 and parts[0] == "customers":
            customer = {"id": parts[1], "email": self.EMAIL, "contact": self.CONTACT}
            return httpx.Response(200, json=customer)
        if request.method == "GET" and len(parts) == 4 and parts[2] == "tokens":
            if parts[3] not in self.token_expiry:
                return _error(400, "BAD_REQUEST_ERROR", "NA")
            return httpx.Response(
                200, json={"id": parts[3], "expired_at": self.token_expiry[parts[3]]}
            )
        return _error(404, "BAD_REQUEST_ERROR", "NA")

    def _create_order(self, body: dict[str, Any]) -> httpx.Response:
        token = body.get("notification", {}).get("token_id")
        if token not in self.tokens:
            return _error(400, "BAD_REQUEST_ERROR", "input_validation_failed")
        created = self.now + len(self.orders)
        order = {
            "id": f"order_fake{len(self.orders) + 1:06d}",
            "entity": "order",
            "amount": body["amount"],
            "currency": body["currency"],
            "receipt": body["receipt"],
            "notes": body["notes"],
            "status": "created",
            "attempts": 0,
            "created_at": created,
            "notification": {
                "id": f"notification_fake{len(self.orders) + 1:06d}",
                "token_id": token,
                "payment_after": created + NOTICE_SECONDS,
                "status": "delivered",
            },
        }
        self.orders.append(order)
        if self.times_out_after_creating:
            self.times_out_after_creating = False
            raise httpx.ReadTimeout("fake timeout after creating the order")
        return httpx.Response(200, json=order)

    def _debit(self, body: dict[str, Any]) -> httpx.Response:
        order = next((o for o in self.orders if o["id"] == body["order_id"]), None)
        if order is None or order["notification"]["token_id"] != body["token"]:
            return _error(400, "BAD_REQUEST_ERROR", "input_validation_failed")
        if not self.notice_delivered:
            return _error(400, "BAD_REQUEST_ERROR", "pre_debit_notification_failed")
        if self.now < order["notification"]["payment_after"]:
            return _error(
                400,
                "BAD_REQUEST_ERROR",
                "input_validation_failed",
                "Debit can be attempted 36 hours after sending the pre-debit notification",
            )
        if self.refuse_debits:
            return _error(400, "BAD_REQUEST_ERROR", "payment_failed")
        payment = self._payment(order, body["notes"])
        order["attempts"] += 1
        return httpx.Response(
            200,
            json={
                "razorpay_payment_id": payment["id"],
                "razorpay_order_id": order["id"],
                "razorpay_signature": "fake",
            },
        )

    def _payment(self, order: dict[str, Any], notes: dict[str, Any]) -> dict[str, Any]:
        items = self.payments.setdefault(order["id"], [])
        payment = {
            "id": f"pay_fake{len(items) + 1:03d}{order['id'][-6:]}",
            "entity": "payment",
            "amount": order["amount"],
            "currency": order["currency"],
            "status": "created",
            "order_id": order["id"],
            "error_code": None,
            "notes": notes,
            "created_at": self.now + len(items),
        }
        items.append(payment)
        return payment


def _listed(order: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in order.items() if k != "notification"}


def _collection(items: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"entity": "collection", "count": len(items), "items": items})


def _error(status: int, code: str, reason: str, description: str = "fake") -> httpx.Response:
    error = {"code": code, "description": description, "reason": reason, "metadata": {}}
    return httpx.Response(status, json={"error": error})
