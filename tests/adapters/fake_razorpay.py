"""A fake Razorpay API behind an httpx transport, shaped by what the real test account
returned in #75 and #83's probes:

- `GET /orders?receipt=` lists orders without their `notification`; `GET /orders/{id}`
  includes it.
- An order's notification sets `payment_after` to 36 h 5 min after the order (ADR-0017).
- A notification for a token Razorpay doesn't know is a 400 `BAD_REQUEST_ERROR`.
"""

import json
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

NOTICE_SECONDS = 36 * 3600 + 5 * 60
CREATED_AT = 1_793_391_196  # 2026-10-30T20:13:16Z, the prototype's notice order


@dataclass
class FakeRazorpay:
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
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def settle(self, receipt: str, status: str = "captured", error_code: str | None = None) -> str:
        """Razorpay makes a payment on the order with the receipt; returns its id."""
        order = next(o for o in self.orders if o["receipt"] == receipt)
        items = self.payments.setdefault(order["id"], [])
        pay_id = f"pay_fake{len(items) + 1:03d}{order['id'][-6:]}"
        items.append(
            {
                "id": pay_id,
                "entity": "payment",
                "amount": order["amount"],
                "currency": order["currency"],
                "status": status,
                "order_id": order["id"],
                "error_code": error_code,
                "notes": order["notes"],
                "created_at": CREATED_AT + 200_000 + len(items),
            }
        )
        return pay_id

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
        if request.method == "GET" and parts == ["orders"]:
            receipt = request.url.params.get("receipt")
            items = [_listed(o) for o in self.orders if o["receipt"] == receipt]
            return httpx.Response(
                200, json={"entity": "collection", "count": len(items), "items": items}
            )
        if request.method == "GET" and len(parts) == 2 and parts[0] == "orders":
            order = next((o for o in self.orders if o["id"] == parts[1]), None)
            return (
                httpx.Response(200, json=order) if order else _error(400, "BAD_REQUEST_ERROR", "NA")
            )
        if request.method == "GET" and len(parts) == 3 and parts[2] == "payments":
            items = self.payments.get(parts[1], [])
            return httpx.Response(
                200, json={"entity": "collection", "count": len(items), "items": items}
            )
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
        created = CREATED_AT + len(self.orders)
        order = {
            "id": f"order_fake{len(self.orders) + 1:06d}",
            "entity": "order",
            "amount": body["amount"],
            "currency": body["currency"],
            "receipt": body["receipt"],
            "notes": body["notes"],
            "status": "created",
            "created_at": created,
            "notification": {
                "id": f"notification_fake{len(self.orders) + 1:06d}",
                "token_id": token,
                "payment_after": created + NOTICE_SECONDS,
                "status": "created",
            },
        }
        self.orders.append(order)
        if self.times_out_after_creating:
            self.times_out_after_creating = False
            raise httpx.ReadTimeout("fake timeout after creating the order")
        return httpx.Response(200, json=order)


def _listed(order: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in order.items() if k != "notification"}


def _error(status: int, code: str, reason: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"code": code, "description": "fake", "reason": reason, "metadata": {}}},
    )
