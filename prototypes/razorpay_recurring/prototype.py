"""PROTOTYPE for #75. Throwaway: never merged, not production code, no tests.

Checks, on the real Razorpay TEST account, the facts PRD #74 relies on for card recurring
payments: debit timing with and without a pre-debit `notification`, which webhooks fire
and what they carry, finding an order by `receipt`, token lifetime, forcing a failure.

    uv run python prototypes/razorpay_recurring/prototype.py serve      # terminal 1
    cloudflared tunnel --url http://localhost:8787                       # terminal 2
    uv run python prototypes/razorpay_recurring/prototype.py register   # then open the URL
    uv run python prototypes/razorpay_recurring/prototype.py token
    uv run python prototypes/razorpay_recurring/prototype.py charge --notify
    uv run python prototypes/razorpay_recurring/prototype.py charge
    uv run python prototypes/razorpay_recurring/prototype.py watch
    uv run python prototypes/razorpay_recurring/prototype.py report

Keys come from .env (AGENTPAY_RAZORPAY_KEY_ID / _KEY_SECRET / _WEBHOOK_SECRET). Live
keys are refused, and no key or secret is ever printed. State is kept in
PROTOTYPE_state.json and PROTOTYPE_events.jsonl beside this file: wipe them freely.
"""

import argparse
import hashlib
import hmac
import json
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

HERE = Path(__file__).resolve().parent
STATE = HERE / "PROTOTYPE_state.json"
EVENTS = HERE / "PROTOTYPE_events.jsonl"
API = "https://api.razorpay.com/v1"
PORT = 8787
NOTE = "agentpay_payment_id"  # the note #81's webhook matcher reads


class Keys(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTPAY_RAZORPAY_", env_file=".env", extra="ignore"
    )

    key_id: str
    key_secret: SecretStr
    webhook_secret: SecretStr | None = None


def keys() -> Keys:
    k = Keys()  # type: ignore[call-arg]
    if not k.key_id.startswith("rzp_test_"):
        sys.exit("Refusing: AGENTPAY_RAZORPAY_KEY_ID must be a test key (rzp_test_...).")
    return k


def now() -> float:
    return time.time()


def stamp(t: float | None = None) -> str:
    return datetime.fromtimestamp(t or now(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> dict[str, Any]:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(state: dict[str, Any]) -> None:
    STATE.write_text(json.dumps(state, indent=2))


def show(label: str, data: Any) -> None:
    print(f"\n== {label} ({stamp()})")
    print(json.dumps(data, indent=2, default=str))


def client() -> httpx.Client:
    k = keys()
    return httpx.Client(
        base_url=API, auth=(k.key_id, k.key_secret.get_secret_value()), timeout=30
    )


def call(c: httpx.Client, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    r = c.request(method, path, json=body)
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        show(f"{method} {path} -> HTTP {r.status_code}", data)
    return data


# --- register: customer + registration order, then Checkout in the browser ---------------


def register(args: argparse.Namespace) -> None:
    state = load()
    with client() as c:
        customer = call(
            c,
            "POST",
            "/customers",
            {
                "name": "Prototype Payer",
                "email": "payer.prototype@example.com",
                "contact": "9000090000",
                "fail_existing": "0",
            },
        )
        show("customer", {k: customer.get(k) for k in ("id", "created_at")})
        order = call(
            c,
            "POST",
            "/orders",
            {
                "amount": 100,
                "currency": "INR",
                "customer_id": customer["id"],
                "method": "card",
                "token": {
                    "max_amount": 500000,
                    "expire_at": int(now()) + 30 * 86400,
                    "frequency": "as_presented",
                },
                "receipt": f"proto_reg_{int(now())}",
                "notes": {"purpose": "AgentPay Guard #75 prototype"},
            },
        )
        show("registration order", order)
    state.update(customer_id=customer["id"], registration_order_id=order["id"])
    save(state)
    url = f"http://localhost:{PORT}/checkout?order_id={order['id']}&customer_id={customer['id']}"
    print(f"\nOpen {url} (the `serve` command must be running) and pay with a recurring")
    print("test card, e.g. Visa 4718 6091 0820 4366, any future expiry, any CVV.")


# --- token: find the token the registration created --------------------------------------


def token(args: argparse.Namespace) -> None:
    state = load()
    with client() as c:
        payments = call(c, "GET", f"/orders/{state['registration_order_id']}/payments")
        items = payments.get("items", [])
        show(
            "registration payments",
            [{k: p.get(k) for k in ("id", "status", "token_id", "recurring")} for p in items],
        )
        tokens = call(c, "GET", f"/customers/{state['customer_id']}/tokens")
        brief = [
            {
                k: t.get(k)
                for k in ("id", "method", "recurring", "recurring_details", "expired_at", "created_at")
            }
            for t in tokens.get("items", [])
        ]
        show("customer tokens", brief)
    token_id = next((p["token_id"] for p in items if p.get("token_id")), None)
    if token_id:
        state.update(token_id=token_id, token_seen_at=stamp())
        save(state)
        print(f"\ntoken {token_id} saved")
    else:
        print("\nno token yet: finish Checkout, or wait for token.confirmed")


# --- charge: a subsequent order and a recurring payment ------------------------------------


def charge(args: argparse.Namespace) -> None:
    state = load()
    label = "notify" if args.notify else "plain"
    payment_id = f"pmt_proto_{label}_{int(now())}"  # stands in for our Payment id
    order_body: dict[str, Any] = {
        "amount": args.amount,
        "currency": "INR",
        "payment_capture": True,
        "receipt": payment_id,
        "notes": {NOTE: payment_id},
    }
    if args.notify:
        order_body["notification"] = {"token_id": state["token_id"]}
    with client() as c:
        order = call(c, "POST", "/orders", order_body)
        show(f"{label} order", order)
        started = now()
        payment = call(
            c,
            "POST",
            "/payments/create/recurring",
            {
                "email": "payer.prototype@example.com",
                "contact": "9000090000",
                "amount": args.amount,
                "currency": "INR",
                "order_id": order.get("id"),
                "customer_id": state["customer_id"],
                "token": state["token_id"],
                "recurring": True,
                "description": f"#75 prototype ({label})",
                "notes": {NOTE: payment_id},
            },
        )
        show(f"{label} recurring payment response", payment)
    runs = state.setdefault("charges", [])
    runs.append(
        {
            "label": label,
            "our_payment_id": payment_id,
            "order_id": order.get("id"),
            "rzp_payment_id": payment.get("razorpay_payment_id") or payment.get("id"),
            "called_at": stamp(started),
            "called_at_epoch": started,
            "statuses": [],
        }
    )
    save(state)


# --- watch: poll every charge until it settles, recording how long it took -----------------


def watch(args: argparse.Namespace) -> None:
    state = load()
    with client() as c:
        while True:
            open_runs = 0
            for run in state.get("charges", []):
                rid = run.get("rzp_payment_id")
                if not rid or run.get("settled"):
                    continue
                p = call(c, "GET", f"/payments/{rid}")
                status = p.get("status")
                seen = [s["status"] for s in run["statuses"]]
                if not seen or seen[-1] != status:
                    elapsed = round(now() - run["called_at_epoch"])
                    run["statuses"].append({"status": status, "after_seconds": elapsed})
                    show(
                        f"{run['label']} {rid} is {status} after {elapsed}s",
                        {k: p.get(k) for k in ("status", "error_code", "error_reason", "order_id")},
                    )
                if status in ("captured", "failed", "refunded"):
                    run["settled"] = True
                else:
                    open_runs += 1
            save(state)
            if not open_runs or args.once:
                break
            time.sleep(args.every)
        # Can an order be found by its receipt? (#74 story 27)
        for run in state.get("charges", []):
            found = call(c, "GET", f"/orders?receipt={run['our_payment_id']}")
            ids = [o.get("id") for o in found.get("items", [])]
            run["found_by_receipt"] = ids
            print(f"orders with receipt {run['our_payment_id']}: {ids}")
        save(state)


# --- serve: the Checkout page and the webhook receiver --------------------------------------

CHECKOUT = """<!doctype html><meta charset=utf-8><title>#75 prototype checkout</title>
<p>PROTOTYPE: authorise a recurring card in Razorpay test mode.</p>
<button id=pay>Open Razorpay Checkout</button><pre id=out></pre>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
document.getElementById('pay').onclick = function () {
  new Razorpay({
    key: "%KEY%", order_id: "%ORDER%", customer_id: "%CUSTOMER%", recurring: true,
    name: "AgentPay Guard prototype", description: "Card mandate registration",
    handler: function (r) {
      document.getElementById('out').textContent = JSON.stringify(r, null, 2);
      fetch('/checkout-done', {method: 'POST', body: JSON.stringify(r)});
    }
  }).open();
};
</script>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path != "/checkout":
            self.send_error(404)
            return
        q = parse_qs(url.query)
        page = (
            CHECKOUT.replace("%KEY%", keys().key_id)  # the key id is public; the secret isn't
            .replace("%ORDER%", q["order_id"][0])
            .replace("%CUSTOMER%", q["customer_id"][0])
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(page.encode())

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/checkout-done":
            show("checkout handler", json.loads(raw or b"{}"))
            self._ok()
            return
        secret = keys().webhook_secret
        expected = (
            hmac.new(secret.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()
            if secret
            else None
        )
        signed = bool(expected) and hmac.compare_digest(
            expected or "", self.headers.get("X-Razorpay-Signature", "")
        )
        body = json.loads(raw or b"{}")
        payload = body.get("payload", {})
        entity = payload.get("payment", {}).get("entity", {})
        order = payload.get("order", {}).get("entity", {})
        token_entity = payload.get("token", {}).get("entity", {})
        record = {
            "received_at": stamp(),
            "signature_ok": signed,
            "event_id": self.headers.get("X-Razorpay-Event-Id"),
            "event": body.get("event"),
            "contains": body.get("contains"),
            "payment_id": entity.get("id"),
            "payment_status": entity.get("status"),
            "error_code": entity.get("error_code"),
            "error_reason": entity.get("error_reason"),
            "order_id": entity.get("order_id") or order.get("id"),
            "order_receipt": order.get("receipt"),
            "payment_notes": entity.get("notes"),
            "token_id": entity.get("token_id") or token_entity.get("id"),
            "token_status": token_entity.get("recurring_details", {}).get("status"),
        }
        with EVENTS.open("a") as f:
            f.write(json.dumps(record) + "\n")
        show(f"webhook {record['event']}", record)
        self._ok()

    def _ok(self) -> None:
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: Any) -> None:  # quiet: we print our own lines
        pass


def serve(args: argparse.Namespace) -> None:
    keys()
    print(f"PROTOTYPE receiver on http://localhost:{PORT} (POST /webhook, GET /checkout)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


# --- report ---------------------------------------------------------------------------------


def report(args: argparse.Namespace) -> None:
    state = load()
    show("charges", state.get("charges", []))
    events = [json.loads(line) for line in EVENTS.read_text().splitlines()] if EVENTS.exists() else []
    show("webhooks in order", events)


def main() -> None:
    parser = argparse.ArgumentParser(description="#75 PROTOTYPE, throwaway")
    sub = parser.add_subparsers(required=True)
    sub.add_parser("serve").set_defaults(run=serve)
    sub.add_parser("register").set_defaults(run=register)
    sub.add_parser("token").set_defaults(run=token)
    p = sub.add_parser("charge")
    p.add_argument("--notify", action="store_true", help="put a notification object on the order")
    p.add_argument("--amount", type=int, default=10000, help="paise (default INR 100.00)")
    p.set_defaults(run=charge)
    p = sub.add_parser("watch")
    p.add_argument("--every", type=int, default=60, help="seconds between polls")
    p.add_argument("--once", action="store_true")
    p.set_defaults(run=watch)
    sub.add_parser("report").set_defaults(run=report)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
