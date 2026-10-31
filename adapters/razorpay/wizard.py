"""Register a Razorpay test card and print the refs the rail contract tests need.

    uv run python -m adapters.razorpay.wizard

Needs AGENTPAY_RAZORPAY_KEY_ID (a test key) and AGENTPAY_RAZORPAY_KEY_SECRET in `.env`.
A person pays ₹1 in Razorpay Checkout with a recurring test card, and Razorpay keeps a
card token for later debits. Test tokens last until 23:59:59 IST the day after they are
made, so re-run this before each contract run. Nothing secret is printed: the page shows
only the key id, which Checkout needs and which is public.
"""

import html
import time
import webbrowser
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

from adapters.razorpay.api import RazorpayApi
from adapters.razorpay.rail import RazorpayRail
from core.domain.rails import RailToken
from core.settings import get_settings

WAIT_MINUTES = 15
IST = timezone(timedelta(hours=5, minutes=30), "IST")
CARD = "Visa 4718 6091 0820 4366, any future expiry date, any CVV"
_PAGE = """<!doctype html><meta charset="utf-8"><title>Register a test card</title>
<p>AgentPay Guard: register a <b>test</b> card with Razorpay for the rail contract tests.</p>
<p>Pay ₹1 with {card}.</p>
<button id="pay">Open Razorpay Checkout</button><p id="done"></p>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
document.getElementById("pay").onclick = function () {{
  new Razorpay({{
    key: "{key}", order_id: "{order}", customer_id: "{customer}", recurring: true,
    name: "AgentPay Guard", description: "Test card registration",
    handler: function () {{
      document.getElementById("done").textContent = "Done. Back to the terminal.";
    }}
  }}).open();
}};
</script>"""


def main() -> None:
    settings = get_settings()
    if settings.razorpay_key_id is None or settings.razorpay_key_secret is None:
        raise SystemExit("Set AGENTPAY_RAZORPAY_KEY_ID and AGENTPAY_RAZORPAY_KEY_SECRET in .env")
    api = RazorpayApi(settings.razorpay_key_id, settings.razorpay_key_secret)

    print("1/3 Making a test customer and a ₹1 registration order...")
    customer = api.post(
        "/customers",
        {
            "name": "AgentPay contract tests",
            "email": "contract.tests@example.com",
            "contact": "9000090000",
            "fail_existing": "0",  # reuse the customer from an earlier run
        },
    )
    order = api.post(
        "/orders",
        {
            "amount": 100,
            "currency": "INR",
            "customer_id": customer["id"],
            "method": "card",
            "payment_capture": True,
            "token": {
                "max_amount": 1_000_000,
                "expire_at": int(time.time()) + 30 * 86_400,
                "frequency": "as_presented",
            },
            "receipt": f"card_registration_{int(time.time())}",
        },
    )

    page = _PAGE.format(
        card=html.escape(CARD),
        key=settings.razorpay_key_id,
        order=order["id"],
        customer=customer["id"],
    ).encode()
    server = _serve(page)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"2/3 Open {url} and pay ₹1 with the recurring test card {CARD}.")
    webbrowser.open(url)

    try:
        token_id = _wait_for_token(api, order["id"])
    finally:
        server.shutdown()
    token = RailToken(
        rail="razorpay", customer_ref=customer["id"], token_ref=token_id, display="test card"
    )
    expires = RazorpayRail.from_settings(settings).token_expires_at(token)
    if expires is None:
        raise SystemExit(f"Razorpay doesn't know token {token_id}; run me again.")

    print("3/3 Registered. Put these in .env:\n")
    print(f"AGENTPAY_RAZORPAY_TEST_CUSTOMER_REF={customer['id']}")
    print(f"AGENTPAY_RAZORPAY_TEST_TOKEN_REF={token_id}\n")
    print(
        f"The token expires {expires:%Y-%m-%d %H:%M} UTC "
        f"({expires.astimezone(IST):%Y-%m-%d %H:%M} IST). Re-run this after that."
    )


def _serve(page: bytes) -> ThreadingHTTPServer:
    class Page(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args: Any) -> None:  # keep the terminal to our own lines
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def _wait_for_token(api: RazorpayApi, order_id: str) -> str:
    deadline = time.monotonic() + WAIT_MINUTES * 60
    while time.monotonic() < deadline:
        for payment in api.get(f"/orders/{order_id}/payments")["items"]:
            if payment.get("token_id"):
                return str(payment["token_id"])
        time.sleep(3)
    raise SystemExit(f"No card was registered within {WAIT_MINUTES} minutes; run me again.")


if __name__ == "__main__":
    main()
