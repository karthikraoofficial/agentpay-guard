"""The first demo journey, over HTTP only, so it runs against any deployment.

1. An Agent pays within its Mandate: allow, then a Payment.
2. A step-up the Payer approves: now Scenario E1 (demo/scenarios/E1.json), which
   `python -m demo --journey 2` plays through the step engine (#276).
3. A dispute signed off and verified: now Scenario G1 (demo/scenarios/G1.json), which
   `python -m demo --journey 3` plays through the step engine (#283).
4. A refund that gives the Monthly cap back: now Scenario H1 (demo/scenarios/H1.json),
   which `python -m demo --journey 4` plays through the step engine (#285).

Journey 1 sets up its own Payer, Agent and Mandate the first time it needs them. A failure
raises StepFailed naming the step. Keys go only into Authorization headers, never into
output."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

Json = dict[str, Any]

GROCER = {"id": "m_freshmart", "name": "FreshMart", "mcc": "5411"}
SMALL = {"minor": 1_250_00, "currency": "INR"}  # low tier: allowed
# The mock rail's test token whose debits capture (adapters/memory/rail.py).
MOCK_CARD = {
    "rail": "mock",
    "customer_ref": "cust_mock",
    "token_ref": "token_mock_captures",
    "display": "visa 1111",
}


class StepFailed(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def money(amount: Json) -> str:
    minor = int(amount["minor"])
    return f"{amount['currency']} {minor // 100:,}.{minor % 100:02d}"


def _problem(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        parts = [str(body[k]) for k in ("code", "title", "detail") if body.get(k)]
        if parts:
            return " - ".join(parts)
    return str(body)[:200]


@dataclass
class Demo:
    client: httpx.Client
    agent_key: str
    tenant_key: str
    say: Callable[[str], None]
    now: Callable[[], datetime]
    mandate_id: str | None = None
    agent_id: str | None = None

    # --- HTTP ---------------------------------------------------------------------------

    def _call(self, step: str, method: str, path: str, key: str, body: Json | None = None) -> Json:
        headers = {"Authorization": f"Bearer {key}"}
        if method == "POST":
            headers["Idempotency-Key"] = f"demo-{uuid.uuid4().hex}"
        try:
            response = self.client.request(method, path, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise StepFailed(step, f"couldn't reach the API ({type(exc).__name__})") from exc
        if response.status_code >= 300:
            raise StepFailed(step, f"HTTP {response.status_code}: {_problem(response)}")
        result: Json = response.json()
        return result

    def _step(self, text: str) -> None:
        self.say(f"  - {text}")

    # --- Shared -------------------------------------------------------------------------

    def _setup(self) -> tuple[str, str]:
        if self.mandate_id is None or self.agent_id is None:
            self.mandate_id, self.agent_id = self._grant(10_000_00, 50_000_00)
        return self.mandate_id, self.agent_id

    def _grant(self, per_txn_minor: int, monthly_minor: int) -> tuple[str, str]:
        """A new Payer, Agent and Mandate with these caps, on the mock card."""
        key = self.tenant_key
        payer = self._call(
            "Register a Payer",
            "POST",
            "/v1/payers",
            key,
            {"display_name": "Asha (demo)", "contact_ref": "demo-payer"},
        )
        agent = self._call("Register an Agent", "POST", "/v1/agents", key, {"name": "ShopBot"})
        now = self.now()
        mandate = self._call(
            "Grant a Mandate",
            "POST",
            "/v1/mandates",
            key,
            {
                "payer_id": payer["payer_id"],
                "allowed_agent_ids": [agent["agent_id"]],
                "currency": "INR",
                "per_txn_cap": {"minor": per_txn_minor, "currency": "INR"},
                "monthly_cap": {"minor": monthly_minor, "currency": "INR"},
                "allowed_mccs": ["5411", "5812"],
                "valid_from": _iso(now - timedelta(minutes=5)),
                "expires_at": _iso(now + timedelta(days=30)),
                "consent_evidence": {
                    "method": "in_app_confirm",
                    "reference": "demo-consent",
                    "captured_at": _iso(now - timedelta(minutes=6)),
                },
                "rail_token": MOCK_CARD,
            },
        )
        cap = {"currency": "INR"}
        self._step(
            f"Payer {payer['payer_id']} granted Mandate {mandate['mandate_id']} to Agent "
            f"{agent['agent_id']}: groceries and restaurants, up to "
            f"{money(cap | {'minor': per_txn_minor})} a payment and "
            f"{money(cap | {'minor': monthly_minor})} a month"
        )
        return mandate["mandate_id"], agent["agent_id"]

    def _decide(self, amount: Json, expected: str) -> Json:
        mandate_id, agent_id = self._setup()
        step = "Ask for a Decision"
        decision = self._call(
            step,
            "POST",
            "/v1/decisions",
            self.agent_key,
            {"mandate_id": mandate_id, "agent_id": agent_id, "amount": amount, "merchant": GROCER},
        )
        reasons = ", ".join(decision["reason_codes"]) or "none"
        self._step(
            f"Agent asks to pay {GROCER['name']} {money(amount)}: {decision['outcome']} "
            f"(Reason codes: {reasons}; Decision {decision['decision_id']})"
        )
        if decision["outcome"] != expected:
            raise StepFailed(step, f"expected {expected}, got {decision['outcome']}")
        return decision

    def _pay(self, decision_id: str) -> Json:
        payment = self._call(
            "Pay the Decision", "POST", "/v1/payments", self.agent_key, {"decision_id": decision_id}
        )
        self._step(f"Payment {payment['payment_id']}: {payment['status']}")
        return payment

    # --- Journeys -----------------------------------------------------------------------

    def journey_1(self) -> Json:
        self.say("Journey 1: an Agent pays within its Mandate")
        decision = self._decide(SMALL, "allow")
        return self._pay(decision["decision_id"])
