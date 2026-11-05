"""The three demo journeys, over HTTP only, so they run against any deployment.

1. An Agent pays within its Mandate: allow, then a Payment.
2. A step-up: the Payer approves through the printed Consent link, then the Payment.
3. A dispute: a captured Payment is disputed, a reviewer signs off its Evidence pack
   through the printed Review link, and the pack is verified against its Sign-off.

Each journey sets up its own Payer, Agent and Mandate the first time it needs them, so any
journey can run on its own. Whenever it waits (for the worker, the Payer or the reviewer)
it calls `wait`, which sleeps in the CLI and plays those parts in the tests. A failure
raises StepFailed naming the step. Keys go only into Authorization headers, never into
output."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import httpx
from pydantic import ValidationError

from core.domain.evidence import EvidencePack

T = TypeVar("T")
Json = dict[str, Any]

GROCER = {"id": "m_freshmart", "name": "FreshMart", "mcc": "5411"}
SMALL = {"minor": 1_250_00, "currency": "INR"}  # low tier: allowed
LARGE = {"minor": 6_000_00, "currency": "INR"}  # over the INR 5,000 step-up threshold
# The mock rail's test token whose debits capture (adapters/memory/rail.py).
MOCK_CARD = {
    "rail": "mock",
    "customer_ref": "cust_mock",
    "token_ref": "token_mock_captures",
    "display": "visa 1111",
}
REVIEWER = {"id": "demo-reviewer", "name": "Demo reviewer"}


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
    wait: Callable[[], None]
    now: Callable[[], datetime]
    # How many times to wait for something before giving up.
    patience: int = 200
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

    def _until(self, step: str, check: Callable[[], T | None]) -> T:
        for _ in range(self.patience):
            found = check()
            if found is not None:
                return found
            self.wait()
        found = check()
        if found is None:
            raise StepFailed(step, f"still waiting after {self.patience} checks")
        return found

    def _step(self, text: str) -> None:
        self.say(f"  - {text}")

    # --- Shared -------------------------------------------------------------------------

    def _setup(self) -> tuple[str, str]:
        if self.mandate_id is not None and self.agent_id is not None:
            return self.mandate_id, self.agent_id
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
                "per_txn_cap": {"minor": 10_000_00, "currency": "INR"},
                "monthly_cap": {"minor": 50_000_00, "currency": "INR"},
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
        self.mandate_id, self.agent_id = mandate["mandate_id"], agent["agent_id"]
        self._step(
            f"Payer {payer['payer_id']} granted Mandate {self.mandate_id} to Agent "
            f"{self.agent_id}: groceries and restaurants, up to INR 10,000.00 a payment and "
            "INR 50,000.00 a month"
        )
        return self.mandate_id, self.agent_id

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

    def journey_2(self) -> Json:
        self.say("Journey 2: a large payment needs the Payer's approval")
        decision = self._decide(LARGE, "step_up")
        csr = decision["step_up"]["consent_request_id"]
        link = self._call(
            "Make a Consent link", "POST", f"/v1/consent-requests/{csr}/link", self.tenant_key
        )
        self._step(f"Consent link (open it and approve as the Payer): {link['url']}")

        step = "Wait for the Payer to approve"

        def resolved() -> str | None:
            got = self._call(step, "GET", f"/v1/consent-requests/{csr}", self.tenant_key)
            status: str = got["status"]
            return None if status == "pending" else status

        status = self._until(step, resolved)
        self._step(f"Consent request {csr}: {status}")
        if status != "approved":
            raise StepFailed(step, f"the Consent request was {status}")
        return self._pay(decision["decision_id"])

    def journey_3(self) -> Json:
        self.say("Journey 3: a captured Payment is disputed, and its evidence signed off")
        decision = self._decide(SMALL, "allow")
        payment = self._pay(decision["decision_id"])
        payment_id = payment["payment_id"]

        step = "Wait for the Payment to be captured"

        def settled() -> str | None:
            got = self._call(step, "GET", f"/v1/payments/{payment_id}", self.agent_key)
            status: str = got["status"]
            return None if status == "scheduled" else status

        status = self._until(step, settled)
        self._step(f"Payment {payment_id}: {status}")
        if status != "captured":
            raise StepFailed(step, f"the Payment {status}")

        opened = self._call(
            "Open a Dispute",
            "POST",
            "/v1/disputes",
            self.tenant_key,
            {
                "payment_id": payment_id,
                "reason": "unauthorised",
                "respond_by": _iso(self.now() + timedelta(days=10)),
            },
        )
        dispute_id = opened["dispute_id"]
        self._step(f"The Payer says it was unauthorised: Dispute {dispute_id} opened")

        step = "Wait for the Evidence pack"

        def built() -> Json | None:
            got = self._call(step, "GET", f"/v1/disputes/{dispute_id}", self.tenant_key)
            return got if got["status"] != "open" else None

        dispute = self._until(step, built)
        if dispute["pack"] is None:
            raise StepFailed(step, f"the Dispute is {dispute['status']} with no pack")
        assessment = dispute["pack"]["assessment"] or {}
        self._step(
            f"Evidence pack v{dispute['pack']['version']}: strength "
            f"{assessment.get('strength', 'n/a')}, Visa {assessment.get('visa_code', 'n/a')}, "
            f"Mastercard {assessment.get('mastercard_code', 'n/a')}, "
            f"{len(dispute['pack']['narrative'])} cited claims, written by "
            f"{'the model' if dispute['pack']['narrative_source'] == 'model' else 'code'}"
        )

        link = self._call(
            "Make a Review link",
            "POST",
            f"/v1/disputes/{dispute_id}/review-link",
            self.tenant_key,
            {"reviewer": REVIEWER},
        )
        self._step(f"Review link (open it and sign off as the reviewer): {link['url']}")

        step = "Wait for the reviewer to sign off"

        def signed() -> Json | None:
            got = self._call(step, "GET", f"/v1/disputes/{dispute_id}", self.tenant_key)
            return got if got["status"] != "in_review" else None

        dispute = self._until(step, signed)
        if dispute["status"] != "ready":
            raise StepFailed(step, f"the Dispute is {dispute['status']}")
        self._step(f"Signed off by {dispute['sign_off']['reviewer']['name']}")
        self._verify(dispute)
        return dispute

    def _verify(self, dispute: Json) -> None:
        step = "Verify the Evidence pack"
        try:
            pack = EvidencePack.model_validate(dispute["pack"])
        except ValidationError as exc:
            raise StepFailed(step, f"the pack doesn't hold together: {exc}") from exc
        sign_off = dispute["sign_off"]
        computed = pack.hash()
        if computed != dispute["pack_hash"] or computed != sign_off["pack_hash"]:
            raise StepFailed(step, "the pack's hash doesn't match its Sign-off")
        if pack.version != sign_off["pack_version"]:
            raise StepFailed(step, "the Sign-off is for another pack version")
        self._step(
            f"Verified: pack v{pack.version} hashes to {computed[:19]}..., the hash the "
            "Sign-off approved, and every claim cites a record in its bundle"
        )
