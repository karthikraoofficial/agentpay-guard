"""The tools the MCP server publishes, each one HTTP call to an existing endpoint.

None reaches a `consent` or `manage` route (ADR-0011): an Agent can ask for a Decision,
never approve one. tests/architecture/test_mcp_scopes.py resolves every tool here to
its route in the API and fails if it does.
"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from core.domain.decisions import AgentPaymentRequest

IDEMPOTENCY_KEY = "idempotency_key"

_IDEMPOTENCY_KEY_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 255,
    "description": (
        "Pick a new unique value for each new purchase. Reuse the same value when you retry "
        "this exact call after a timeout or error, so you get the first answer back instead "
        "of a second one."
    ),
}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    method: Literal["GET", "POST"]
    # The API route's path, exactly as FastAPI declares it, e.g. /v1/decisions/{decision_id}.
    path: str
    # The API's request body model, for a write. A write also needs an idempotency key.
    body: type[BaseModel] | None = None
    path_params: tuple[str, ...] = ()

    @property
    def writes(self) -> bool:
        return self.body is not None

    def input_schema(self) -> dict[str, Any]:
        """The API's request model, so the two can't drift, plus `idempotency_key` on a
        write and the path parameters on a read. Never a Tenant id: the key names it."""
        if self.body is not None:
            schema = self.body.model_json_schema()
            if IDEMPOTENCY_KEY in schema["properties"]:
                raise ValueError(f"{self.body.__name__} already has an {IDEMPOTENCY_KEY}")
            schema["properties"][IDEMPOTENCY_KEY] = _IDEMPOTENCY_KEY_SCHEMA
            schema["required"] = [*schema.get("required", []), IDEMPOTENCY_KEY]
            return schema
        return {
            "type": "object",
            "properties": {p: {"type": "string", "minLength": 1} for p in self.path_params},
            "required": list(self.path_params),
            "additionalProperties": False,
        }


STEP_UP_NOTE = (
    "step_up: a person must approve this payment before you may pay. Don't pay, retry or "
    "try another way, and don't ask for a new Decision to get round it. Nothing you can call "
    "approves it: only the Payer can. Wait, then check step_up.consent_request_id with "
    "get_consent_request. Once it is approved, read the Decision with get_decision for "
    "payable_until, and pay before then."
)

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="request_decision",
        description=(
            "Ask AgentPay Guard whether you may make this payment. Call it before every "
            "payment. The answer's outcome is allow (you may pay, until payable_until), "
            "step_up (a person must approve first: don't pay yet), or block (don't pay; "
            "reason_codes say why). Keep the decision_id: you need it to read the Decision "
            "again and to pay. The idempotency key makes a retry safe: reuse it when you "
            "retry the same request, and pick a new one for a new purchase."
        ),
        method="POST",
        path="/v1/decisions",
        body=AgentPaymentRequest,
    ),
    Tool(
        name="get_decision",
        description=(
            "Read a Decision you were given earlier, by its decision_id. On a step_up, "
            "consent.status shows whether the Payer has approved, declined, or not yet "
            "answered, and payable_until is set once it is approved."
        ),
        method="GET",
        path="/v1/decisions/{decision_id}",
        path_params=("decision_id",),
    ),
    Tool(
        name="get_consent_request",
        description=(
            "Check whether the Payer has answered a step_up, by the consent_request_id the "
            "step_up gave you. status is pending (not answered yet: wait and check again "
            "later), approved (read the Decision with get_decision for payable_until, then "
            "you may pay), declined or expired (don't pay), or blocked (the Mandate no "
            "longer allows the payment: don't pay). Only the Payer can approve; no tool here "
            "can."
        ),
        method="GET",
        path="/v1/consent-requests/{consent_request_id}",
        path_params=("consent_request_id",),
    ),
)
