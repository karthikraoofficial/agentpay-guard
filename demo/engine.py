"""The step engine (#273): runs a Scenario one step at a time, over HTTP only, so the Demo
runner, `python -m demo --scenario` and the tests run journeys the same way.

A run first grants its own Payer, Agent and Mandate (the setup step), so concurrent
visitors never share Spent this month, Velocity or New merchant history. Agent steps go
through the guard_mcp tool layer with the decide key, exactly as an Agent in an MCP host
would; setup goes through the API's routes with the tenant key. Each step that writes
gets an idempotency key of its own, and a retried step reuses it, so a double click or a
lost response never makes a second Decision or Payment.

`next()` runs exactly one step and returns its Card. A step that waits on the worker or
a person (an Agent read with `until_status`) answers a waiting Card and runs again on
the next call. The last Card says whether the run ended as the Scenario expects.

Keys live only in the two HTTP clients' Authorization headers: never in a Card, a log
line or an error.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from demo.scenario import AgentStep, ChatLine, Scenario
from guard_mcp.server import call
from guard_mcp.tools import TOOLS

Json = dict[str, Any]

_TOOLS = {tool.name: tool for tool in TOOLS}
# What this engine can run so far; a Scenario that needs more is refused up front.
_AGENT_TOOLS = {"request_decision", "create_payment", "get_payment"}
_CHECKED = {"outcome", "reason_codes", "payment_status"}
# The mock rail's test customer (adapters/memory/rail.py).
_MOCK_CUSTOMER = "cust_mock"


class StepFailed(Exception):
    """A step couldn't run: names the step and why. Never holds a key."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


class WrongStep(Exception):
    """The caller asked for a step other than the next one or the one just run."""


class Unsupported(ValueError):
    """The Scenario needs a step kind or check this engine doesn't run yet."""


class Call(BaseModel):
    """One Guard call: the guard_mcp tool or the HTTP route, what was sent and the answer."""

    kind: str  # "tool" or "route"
    name: str
    request: Json | None
    response: Json


class DecisionView(BaseModel):
    decision_id: str
    outcome: str
    reason_codes: list[str]
    policy_pack: str  # e.g. default@1.5.0
    pack_hash: str
    audit_record_id: str


class Card(BaseModel):
    """What one step did, for the chat, the inspector and the CLI."""

    step: int
    actor: str
    chat: list[ChatLine]
    call: Call | None
    decision: DecisionView | None = None
    # The step is waiting on the worker or a person: ask for the same step again.
    waiting: bool = False
    finished: bool = False
    # Set on the last Card: how the run differs from the Scenario's expected result.
    mismatches: list[str] = []


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _problem(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        parts = [str(body[k]) for k in ("code", "detail") if body.get(k)]
        if parts:
            return f"HTTP {response.status_code}: " + " - ".join(parts)
    return f"HTTP {response.status_code}"


def check_supported(scenario: Scenario) -> None:
    """Raise Unsupported if this engine can't run the Scenario yet."""
    problems = []
    if scenario.setup.history:
        problems.append("setup history")
    for step in scenario.steps:
        if not isinstance(step, AgentStep):
            problems.append(f"{step.actor} steps")
        elif step.tool not in _AGENT_TOOLS:
            problems.append(f"the {step.tool} tool")
    if scenario.demo_controls:
        problems.append("Demo controls")
    unchecked = scenario.expected.model_fields_set - _CHECKED
    problems += [f"checking {name}" for name in sorted(unchecked)]
    if problems:
        raise Unsupported(f"Scenario {scenario.id} needs {', '.join(problems)}")


class Run:
    def __init__(
        self,
        scenario: Scenario,
        *,
        agent: httpx.AsyncClient,
        tenant: httpx.AsyncClient,
        now: Callable[[], datetime],
    ) -> None:
        check_supported(scenario)
        self.scenario = scenario
        self._agent = agent
        self._tenant = tenant
        self._now = now
        # Step 0 is the setup; Scenario step i is step i + 1.
        self.total = len(scenario.steps) + 1
        self.index = 0
        self._keys: dict[int, str] = {}
        self._last: Card | None = None
        self.mandate_id: str | None = None
        self.agent_id: str | None = None
        self.decision: Json | None = None
        self.payment: Json | None = None

    @property
    def finished(self) -> bool:
        return self.index >= self.total

    def _key(self, index: int) -> str:
        """The step's idempotency key: made once, reused on every retry of that step."""
        return self._keys.setdefault(index, f"demo-{uuid.uuid4().hex}")

    async def next(self, step: int | None = None) -> Card:
        """Run one step. `step`, when given, is the step the caller means: the one just
        run (a retried request) gets its Card back without running anything again."""
        if step is not None and self._last is not None and step == self._last.step:
            if not self._last.waiting:
                return self._last
        elif step is not None and step != self.index:
            raise WrongStep(f"This run is at step {self.index}, not {step}.")
        if self.finished:
            if self._last is None:  # pragma: no cover - total is never 0
                raise StepFailed("Next step", "the run has no steps")
            return self._last
        index = self.index
        if index == 0:
            card = await self._setup()
        else:
            scenario_step = self.scenario.steps[index - 1]
            if not isinstance(scenario_step, AgentStep):  # refused in check_supported
                raise Unsupported(f"{scenario_step.actor} steps")
            card = await self._agent_step(index, scenario_step)
        if not card.waiting:
            self.index += 1
        if self.finished:
            card = card.model_copy(update={"finished": True, "mismatches": self.mismatches()})
        self._last = card
        return card

    # --- Setup ----------------------------------------------------------------------------

    async def _route(self, step: str, path: str, body: Json, key: str) -> Json:
        try:
            response = await self._tenant.post(path, json=body, headers={"Idempotency-Key": key})
        except httpx.HTTPError as exc:
            raise StepFailed(step, f"couldn't reach the API ({type(exc).__name__})") from None
        if not response.is_success:
            raise StepFailed(step, _problem(response))
        result: Json = response.json()
        return result

    async def _setup(self) -> Card:
        step = "Grant a Mandate"
        key = self._key(0)
        setup = self.scenario.setup
        payer = await self._route(
            step,
            "/v1/payers",
            {"display_name": "Asha (demo)", "contact_ref": "demo-payer"},
            f"{key}-payer",
        )
        agent = await self._route(step, "/v1/agents", {"name": "ShopBot"}, f"{key}-agent")
        now = self._now()
        terms = setup.mandate
        mandate_body: Json = {
            "payer_id": payer["payer_id"],
            "allowed_agent_ids": [agent["agent_id"]],
            "currency": terms.per_txn_cap.currency,
            "per_txn_cap": terms.per_txn_cap.model_dump(),
            "monthly_cap": terms.monthly_cap.model_dump(),
            "allowed_mccs": terms.allowed_mccs,
            "valid_from": _iso(now - timedelta(minutes=5)),
            "expires_at": _iso(now + timedelta(days=terms.valid_for_days)),
            "consent_evidence": {
                "method": "in_app_confirm",
                "reference": "demo-consent",
                "captured_at": _iso(now - timedelta(minutes=6)),
            },
            "rail_token": {
                "rail": "mock",
                "customer_ref": _MOCK_CUSTOMER,
                "token_ref": setup.card,
                "display": "visa 1111",
            },
        }
        mandate = await self._route(step, "/v1/mandates", mandate_body, f"{key}-mandate")
        self.mandate_id = mandate["mandate_id"]
        self.agent_id = agent["agent_id"]
        return Card(
            step=0,
            actor="tenant",
            chat=[],
            call=Call(
                kind="route", name="POST /v1/mandates", request=mandate_body, response=mandate
            ),
        )

    # --- Agent steps ----------------------------------------------------------------------

    def _arguments(self, index: int, step: AgentStep) -> Json:
        if step.tool == "request_decision":
            if step.amount is None:
                raise StepFailed("request_decision", "the step names no amount")
            return {
                "mandate_id": self.mandate_id,
                "agent_id": self.agent_id,
                "amount": step.amount.model_dump(),
                "merchant": self.scenario.setup.merchant.model_dump(),
                "idempotency_key": self._key(index),
            }
        if step.tool == "create_payment":
            if self.decision is None:
                raise StepFailed("create_payment", "there is no Decision to pay yet")
            return {
                "decision_id": self.decision["decision_id"],
                "idempotency_key": self._key(index),
            }
        # get_payment
        if self.payment is None:
            raise StepFailed("get_payment", "there is no Payment to read yet")
        return {"payment_id": self.payment["payment_id"]}

    async def _agent_step(self, index: int, step: AgentStep) -> Card:
        arguments = self._arguments(index, step)
        result = await call(self._agent, _TOOLS[step.tool], arguments)
        body: Json = dict(result.structured_content or {})
        if result.is_error:
            code = body.get("code", "TOOL_ERROR")
            raise StepFailed(step.tool, f"{code}: {body.get('detail', '')}".rstrip(": "))
        decision = None
        if "outcome" in body:
            self.decision = body
            decision = _decision_view(body)
        if "payment_id" in body:
            self.payment = body
        waiting = step.until_status is not None and body.get("status") not in step.until_status
        request = {k: v for k, v in arguments.items() if k != "idempotency_key"}
        return Card(
            step=index,
            actor="agent",
            chat=step.chat,
            call=Call(kind="tool", name=step.tool, request=request, response=body),
            decision=decision,
            waiting=waiting,
        )

    # --- The expected result ----------------------------------------------------------------

    def mismatches(self) -> list[str]:
        expected = self.scenario.expected
        found: list[str] = []
        decision = self.decision or {}
        payment = self.payment or {}
        if expected.outcome is not None and decision.get("outcome") != expected.outcome:
            found.append(f"outcome: expected {expected.outcome}, got {decision.get('outcome')}")
        if expected.reason_codes is not None:
            got = list(decision.get("reason_codes", []))
            if sorted(got) != sorted(expected.reason_codes):
                found.append(f"reason codes: expected {expected.reason_codes}, got {got}")
        if expected.payment_status is not None and payment.get("status") != expected.payment_status:
            found.append(
                f"payment status: expected {expected.payment_status}, got {payment.get('status')}"
            )
        return found


def _decision_view(body: Json) -> DecisionView:
    pack = body["policy_pack"]
    return DecisionView(
        decision_id=body["decision_id"],
        outcome=body["outcome"],
        reason_codes=list(body["reason_codes"]),
        policy_pack=f"{pack['id']}@{pack['version']}",
        pack_hash=pack["hash"],
        audit_record_id=body["audit_record_id"],
    )


async def play(run: Run, wait: Callable[[], None], patience: int = 200) -> list[Card]:
    """Run every step, calling `wait` whenever a step waits (it sleeps in the CLI and plays
    the worker and people in tests). Gives up on a step after `patience` waits."""
    cards: list[Card] = []
    waits = 0
    while True:
        card = await run.next()
        cards.append(card)
        if card.finished:
            return cards
        if card.waiting:
            waits += 1
            if waits > patience:
                raise StepFailed(
                    card.call.name if card.call else f"step {card.step}",
                    f"still waiting after {patience} checks",
                )
            wait()
        else:
            waits = 0
