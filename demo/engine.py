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

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from demo.scenario import AgentStep, ChatLine, HumanStep, Scenario, TenantStep
from guard_mcp.server import call
from guard_mcp.tools import TOOLS

Json = dict[str, Any]
# Who answers a Payer step: a visitor on the consent screen (the Demo runner and the CLI
# wait for them), or the engine itself (CI), on the same Consent link.
People = Literal["visitor", "engine"]

_TOOLS = {tool.name: tool for tool in TOOLS}
# What this engine can run so far; a Scenario that needs more is refused up front.
_AGENT_TOOLS = {
    "request_decision",
    "get_decision",
    "get_consent_request",
    "create_payment",
    "get_payment",
}
_PAYER_ACTIONS = {"approve", "decline"}
_CHECKED = {"outcome", "reason_codes", "payment_status", "consent_status", "refusal_code"}
# The mock rail's test customer (adapters/memory/rail.py).
_MOCK_CUSTOMER = "cust_mock"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


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


class ConsentView(BaseModel):
    consent_request_id: str
    status: str
    # Only when an approval was blocked: the re-check's Reason codes.
    reason_codes: list[str] | None = None


class Card(BaseModel):
    """What one step did, for the chat, the inspector and the CLI."""

    step: int
    actor: str
    chat: list[ChatLine]
    call: Call | None
    decision: DecisionView | None = None
    # A Payer step: the Consent link Asha answers on (the consent screen, ADR-0008).
    link: str | None = None
    # The Consent request as this step left it.
    consent: ConsentView | None = None
    # The error code of a refusal the journey expects, e.g. INSUFFICIENT_SCOPE.
    refused: str | None = None
    # The step is waiting on the worker or a person: ask for the same step again.
    waiting: bool = False
    finished: bool = False
    # Set on the last Card: how the run differs from the Scenario's expected result.
    mismatches: list[str] = []


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _body(response: httpx.Response) -> Json:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _problem(response: httpx.Response) -> str:
    body = _body(response)
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
        if isinstance(step, AgentStep) and step.tool is not None:
            if step.tool not in _AGENT_TOOLS:
                problems.append(f"the {step.tool} tool")
        elif isinstance(step, HumanStep):
            if step.actor != "payer" or step.action not in _PAYER_ACTIONS:
                problems.append(f"{step.actor} {step.action} steps")
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
        people: People = "visitor",
    ) -> None:
        check_supported(scenario)
        self.scenario = scenario
        self._agent = agent
        self._tenant = tenant
        self._now = now
        self.people = people
        # Step 0 is the setup; Scenario step i is step i + 1.
        self.total = len(scenario.steps) + 1
        self.index = 0
        self._keys: dict[int, str] = {}
        self._links: dict[int, str] = {}
        self._last: Card | None = None
        self.mandate_id: str | None = None
        self.agent_id: str | None = None
        self.decision: Json | None = None
        self.payment: Json | None = None
        self.consent: Json | None = None
        self.refusal: str | None = None
        self._problems: list[str] = []

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
            if isinstance(scenario_step, HumanStep):
                card = await self._payer_step(index, scenario_step)
            elif isinstance(scenario_step, AgentStep) and scenario_step.tool is not None:
                card = await self._tool_step(index, scenario_step)
            else:
                card = await self._route_step(index, scenario_step)
        if not card.waiting:
            self.index += 1
        if self.finished:
            card = card.model_copy(update={"finished": True, "mismatches": self.mismatches()})
        self._last = card
        return card

    # --- HTTP -----------------------------------------------------------------------------

    async def _send(
        self,
        client: httpx.AsyncClient,
        step: str,
        method: str,
        path: str,
        body: Json | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return await client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise StepFailed(step, f"couldn't reach the API ({type(exc).__name__})") from None

    async def _post(self, step: str, path: str, body: Json, key: str) -> Json:
        response = await self._send(
            self._tenant, step, "POST", path, body, {"Idempotency-Key": key}
        )
        if not response.is_success:
            raise StepFailed(step, _problem(response))
        return _body(response)

    def _fill(self, route: str) -> tuple[str, str]:
        """`POST /v1/mandates/{mandate_id}/revoke` -> ("POST", "/v1/mandates/man_.../revoke")."""
        known = {
            "mandate_id": self.mandate_id,
            "agent_id": self.agent_id,
            "decision_id": (self.decision or {}).get("decision_id"),
            "payment_id": (self.payment or {}).get("payment_id"),
            "consent_request_id": self._consent_request_id(),
        }

        def value(match: re.Match[str]) -> str:
            found = known.get(match.group(1))
            if not found:
                raise StepFailed(route, f"the run has no {match.group(1)} yet")
            return str(found)

        method, path = route.split(" ", 1)
        return method, _PLACEHOLDER.sub(value, path)

    def _consent_request_id(self) -> str | None:
        step_up = (self.decision or {}).get("step_up") or {}
        found = step_up.get("consent_request_id")
        return str(found) if found else None

    def _remember(self, body: Json) -> DecisionView | None:
        """Keep what the run needs from an answer; the Decision's view if it is one."""
        if "outcome" in body:
            self.decision = body
            return _decision_view(body)
        if "payment_id" in body:
            self.payment = body
        elif "consent_request_id" in body and "status" in body:
            self.consent = body
        return None

    # --- Setup ----------------------------------------------------------------------------

    async def _setup(self) -> Card:
        step = "Grant a Mandate"
        key = self._key(0)
        setup = self.scenario.setup
        payer = await self._post(
            step,
            "/v1/payers",
            {"display_name": "Asha (demo)", "contact_ref": "demo-payer"},
            f"{key}-payer",
        )
        agent = await self._post(step, "/v1/agents", {"name": "ShopBot"}, f"{key}-agent")
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
        mandate = await self._post(step, "/v1/mandates", mandate_body, f"{key}-mandate")
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

    # --- Agent tool steps -----------------------------------------------------------------

    def _arguments(self, index: int, step: AgentStep) -> Json:
        tool = step.tool
        if tool == "request_decision":
            if step.amount is None:
                raise StepFailed(tool, "the step names no amount")
            return {
                "mandate_id": self.mandate_id,
                "agent_id": self.agent_id,
                "amount": step.amount.model_dump(),
                "merchant": self.scenario.setup.merchant.model_dump(),
                "idempotency_key": self._key(index),
            }
        if tool in ("create_payment", "get_decision"):
            if self.decision is None:
                raise StepFailed(str(tool), "there is no Decision yet")
            arguments: Json = {"decision_id": self.decision["decision_id"]}
            if tool == "create_payment":
                arguments["idempotency_key"] = self._key(index)
            return arguments
        if tool == "get_consent_request":
            consent_request_id = self._consent_request_id()
            if consent_request_id is None:
                raise StepFailed(tool, "the Decision has no Consent request")
            return {"consent_request_id": consent_request_id}
        # get_payment
        if self.payment is None:
            raise StepFailed("get_payment", "there is no Payment to read yet")
        return {"payment_id": self.payment["payment_id"]}

    async def _tool_step(self, index: int, step: AgentStep) -> Card:
        tool = str(step.tool)
        arguments = self._arguments(index, step)
        result = await call(self._agent, _TOOLS[tool], arguments)
        body: Json = dict(result.structured_content or {})
        refused = None
        if result.is_error:
            code = str(body.get("code", "TOOL_ERROR"))
            if code != step.expect_refusal:
                raise StepFailed(tool, f"{code}: {body.get('detail', '')}".rstrip(": "))
            refused = self.refusal = code
        elif step.expect_refusal:
            raise StepFailed(tool, f"expected {step.expect_refusal}, but it was allowed")
        decision = self._remember(body) if refused is None else None
        waiting = step.until_status is not None and body.get("status") not in step.until_status
        request = {k: v for k, v in arguments.items() if k != "idempotency_key"}
        return Card(
            step=index,
            actor="agent",
            chat=step.chat,
            call=Call(kind="tool", name=tool, request=request, response=body),
            decision=decision,
            consent=_consent_view(body) if "outcome" not in body else None,
            refused=refused,
            waiting=waiting,
        )

    # --- Route steps: the wallet app, or ShopBot calling a route no tool reaches -----------

    async def _route_step(self, index: int, step: AgentStep | TenantStep) -> Card:
        route = str(step.route)
        method, path = self._fill(route)
        body = step.body if isinstance(step, TenantStep) else None
        if method == "POST" and body is None:
            body = {}
        headers = {"Idempotency-Key": self._key(index)} if method == "POST" else None
        client = self._agent if step.actor == "agent" else self._tenant
        response = await self._send(client, route, method, path, body, headers)
        answer = _body(response)
        refused = None
        if not response.is_success:
            code = str(answer.get("code", f"HTTP_{response.status_code}"))
            if code != step.expect_refusal:
                raise StepFailed(route, _problem(response))
            refused = self.refusal = code
        elif step.expect_refusal:
            raise StepFailed(route, f"expected {step.expect_refusal}, but it was allowed")
        decision = self._remember(answer) if refused is None else None
        return Card(
            step=index,
            actor=step.actor,
            chat=step.chat,
            call=Call(kind="route", name=route, request=body, response=answer),
            decision=decision,
            refused=refused,
        )

    # --- Payer steps ----------------------------------------------------------------------

    async def _link(self, index: int) -> tuple[str, str]:
        """The Consent request and its Consent link, minted once for this step."""
        consent_request_id = self._consent_request_id()
        if consent_request_id is None:
            raise StepFailed("Asha's approval", "the Decision has no Consent request")
        if index not in self._links:
            minted = await self._post(
                "Make a Consent link",
                f"/v1/consent-requests/{consent_request_id}/link",
                {},
                f"{self._key(index)}-link",
            )
            if not minted.get("url"):
                raise StepFailed("Make a Consent link", "the link's answer was lost; start again")
            self._links[index] = str(minted["url"])
        return consent_request_id, self._links[index]

    async def _payer_step(self, index: int, step: HumanStep) -> Card:
        consent_request_id, link = await self._link(index)
        if self.people == "engine":
            return await self._tap(index, step, link)
        # The Decision read carries the Consent request's status and, when an approval
        # was blocked, the re-check's Reason codes.
        decision_id = (self.decision or {}).get("decision_id")
        path = f"/v1/decisions/{decision_id}"
        response = await self._send(self._tenant, "Asha's answer", "GET", path)
        if not response.is_success:
            raise StepFailed("Asha's answer", _problem(response))
        consent = {
            **(_body(response).get("consent") or {}),
            "consent_request_id": consent_request_id,
        }
        self.consent = consent
        return Card(
            step=index,
            actor="payer",
            chat=step.chat,
            call=Call(kind="route", name=f"GET {path}", request=None, response=consent),
            link=link,
            consent=_consent_view(consent),
            waiting=consent.get("status") == "pending",
        )

    async def _tap(self, index: int, step: HumanStep, link: str) -> Card:
        """Asha answers on her Consent link, `taps` times with one idempotency key."""
        token = link.split("#", 1)[1]
        route = f"/v1/consent-link/{step.action}"
        headers = {"Consent-Link": token, "Idempotency-Key": f"{self._key(index)}-tap"}
        answers = []
        for _ in range(step.taps):
            response = await self._send(self._tenant, "Asha's answer", "POST", route, None, headers)
            if not response.is_success:
                raise StepFailed("Asha's answer", _problem(response))
            answers.append(_body(response))
        distinct = len({json.dumps(a, sort_keys=True) for a in answers})
        if distinct != 1:
            self._problems.append(f"Asha's {step.taps} taps made {distinct} different answers")
        self._remember(answers[0])
        return Card(
            step=index,
            actor="payer",
            chat=step.chat,
            call=Call(
                kind="route",
                name=f"POST {route}",
                request=None,
                response={"taps": step.taps, "distinct_answers": distinct, "answer": answers[0]},
            ),
            link=link,
            consent=_consent_view(answers[0]),
        )

    # --- The expected result ----------------------------------------------------------------

    def mismatches(self) -> list[str]:
        expected = self.scenario.expected
        found = list(self._problems)
        decision = self.decision or {}

        def check(name: str, want: object, got: object) -> None:
            if want is not None and got != want:
                found.append(f"{name}: expected {want}, got {got}")

        check("outcome", expected.outcome, decision.get("outcome"))
        if expected.reason_codes is not None:
            got = sorted(decision.get("reason_codes", []))
            check("reason codes", sorted(expected.reason_codes), got)
        check("payment status", expected.payment_status, (self.payment or {}).get("status"))
        check("consent status", expected.consent_status, (self.consent or {}).get("status"))
        check("refusal", expected.refusal_code, self.refusal)
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


def _consent_view(body: Json) -> ConsentView | None:
    if "consent_request_id" not in body or "status" not in body:
        return None
    codes = body.get("reason_codes")
    return ConsentView(
        consent_request_id=str(body["consent_request_id"]),
        status=str(body["status"]),
        reason_codes=list(codes) if codes else None,
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
