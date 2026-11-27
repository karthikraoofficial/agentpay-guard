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
from typing import Any, Literal, get_args

import httpx
from pydantic import BaseModel

from core.domain.decisions import Merchant
from core.domain.money import Money
from demo.scenario import (
    REGISTER_AGENT,
    AgentStep,
    ChatLine,
    FastForward,
    HumanStep,
    Scenario,
    TenantStep,
    VisitorStep,
)
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
    "check_intent",
    "get_decision",
    "get_consent_request",
    "create_payment",
    "get_payment",
}
# Who the run's Review links name: the Tenant attests who reviews (Reviewer).
_REVIEWER = {"id": "demo-reviewer", "name": "Demo reviewer"}
_CHECKED = {
    "outcome",
    "reason_codes",
    "payment_status",
    "payment_failure_code",
    "consent_status",
    "refusal_code",
    "replay_status",
    "chain_clean",
    "dispute_status",
    "evidence_strength",
    "narrative_source",
    "pack_clean",
}
_FAST_FORWARDS = set(get_args(FastForward))
# Each fast-forward's Demo control route (ADR-0032) and the run's id it ends.
_CONTROL_ROUTES = {
    "expire_consent": "POST /v1/demo/consent-requests/{consent_request_id}/expire",
    "expire_payment_window": "POST /v1/demo/decisions/{decision_id}/expire-payment-window",
    "expire_notice": "POST /v1/demo/payments/{payment_id}/expire-notice",
    "ai_off": "POST /v1/demo/mandates/{mandate_id}/ai-off",
    "rail_declining": "POST /v1/demo/mandates/{mandate_id}/rail-declining",
    "lose_payment": "POST /v1/demo/mandates/{mandate_id}/lose-payment",
    "rail_down": "POST /v1/demo/rail-down",
    "tamper_copy": "GET /v1/demo/audit-records/{audit_record_id}/tampered-copy",
}
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


class RuleView(BaseModel):
    rule_id: str
    result: str
    reason_code: str | None = None


class DecisionView(BaseModel):
    decision_id: str
    outcome: str
    reason_codes: list[str]
    policy_pack: str  # e.g. default@1.5.0
    pack_hash: str
    audit_record_id: str
    # Every rule that ran, from the Audit record. Empty if that read failed: the Decision
    # stands either way, and the inspector says the rules aren't available.
    rule_results: list[RuleView] = []


class ReplayView(BaseModel):
    audit_record_id: str
    seq: int
    status: str
    identical: bool
    difference: str | None = None
    detail: str | None = None


class ChainView(BaseModel):
    clean: bool
    record_count: int
    first_broken_seq: int | None = None


class PackVersionView(BaseModel):
    version: int
    signed_off: bool
    problems: list[str]


class PackCheckView(BaseModel):
    """verify-pack's answer (#283): each Evidence pack version against the Audit chain
    and its Sign-off, then the chain."""

    dispute_id: str
    status: str  # clean, different or pack_problem
    clean: bool
    versions: list[PackVersionView]
    pack_problems: list[str] = []


class Verification(BaseModel):
    """The inspector's Verify / Replay button: both answers (#275), and verify-pack's
    when the run has a Dispute (#283)."""

    replay: ReplayView
    chain: ChainView
    pack: PackCheckView | None = None


class DisputeView(BaseModel):
    """A Dispute as the Tenant reads it, with its latest Evidence pack in brief."""

    dispute_id: str
    status: str
    pack_version: int | None = None
    pack_hash: str | None = None
    evidence_strength: str | None = None
    # Who wrote the narrative (model or template) and, for code standing in for the
    # model, why (e.g. "model narrative unavailable").
    narrative_source: str | None = None
    narrative_note: str | None = None
    signed_off_version: int | None = None
    reviewer: str | None = None


class IntentView(BaseModel):
    """A cart check as the Tenant reads it: the tool tells the Agent only the check's id,
    never its verdict (api/routes/intents.py), so the engine reads it with the tenant key."""

    intent_id: str
    verdict: str
    # Why the check came back unknown without the model's word, e.g. capped (the Model
    # spend cap), disabled (the AI is off), timeout or error.
    failure_cause: str | None = None
    # The verdict the Scenario scripted for this check, which CI plays (#279).
    scripted: str | None = None
    # The live verdict isn't the scripted one: the run carries on with the live answer.
    differs: bool = False


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
    # A cart check step: its verdict. None if that read failed.
    intent: IntentView | None = None
    # A Dispute step: the Dispute as this step left it.
    dispute: DisputeView | None = None
    # A verify-pack step: its answer.
    pack_check: PackCheckView | None = None
    # A Payer step: the Consent link Asha answers on (the consent screen, ADR-0008). A
    # reviewer step: the Review link the reviewer acts on (the dispute viewer).
    link: str | None = None
    # The Consent request as this step left it.
    consent: ConsentView | None = None
    # The error code of a refusal the journey expects, e.g. INSUFFICIENT_SCOPE.
    refused: str | None = None
    # A Demo control waiting for the visitor's press: the button's words.
    fast_forward: str | None = None
    # That control forces a failure (a switch) rather than ending a timer.
    switch: bool = False
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
    for step in scenario.steps:
        if isinstance(step, AgentStep) and step.tool is not None:
            if step.tool not in _AGENT_TOOLS:
                problems.append(f"the {step.tool} tool")
    if unknown := set(scenario.demo_controls) - _FAST_FORWARDS:
        problems.append(f"the Demo controls {sorted(unknown)}")
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
        # Other Agents a Tenant step registered, by name, for `as_agent` (B6).
        self._agents: dict[str, str] = {}
        self.decision: Json | None = None
        self.payment: Json | None = None
        self.consent: Json | None = None
        self.refusal: str | None = None
        self.intent_id: str | None = None
        self.replayed: Json | None = None
        self.chain: Json | None = None
        self.dispute: Json | None = None
        self.pack_check: Json | None = None
        # The run's one Review link: a Regenerate through it keeps it working, so every
        # reviewer step uses the link the visitor already has open.
        self._review_link: str | None = None
        self._rules: dict[str, list[RuleView]] = {}
        self._problems: list[str] = []

    @property
    def finished(self) -> bool:
        return self.index >= self.total

    def _key(self, index: int) -> str:
        """The step's idempotency key: made once, reused on every retry of that step."""
        return self._keys.setdefault(index, f"demo-{uuid.uuid4().hex}")

    async def next(self, step: int | None = None, *, press: bool = False) -> Card:
        """Run one step. `step`, when given, is the step the caller means: the one just
        run (a retried request) gets its Card back without running anything again.
        `press` is the visitor pressing a fast-forward this step is waiting on."""
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
            if isinstance(scenario_step, HumanStep) and scenario_step.actor == "reviewer":
                card = await self._reviewer_step(index, scenario_step)
            elif isinstance(scenario_step, HumanStep):
                card = await self._payer_step(index, scenario_step)
            elif isinstance(scenario_step, VisitorStep):
                card = await self._fast_forward(index, scenario_step, press)
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

    def _known(self, name: str, where: str) -> str:
        """A `{name}` placeholder's value from the run."""
        known = {
            "mandate_id": self.mandate_id,
            "agent_id": self.agent_id,
            "decision_id": (self.decision or {}).get("decision_id"),
            "audit_record_id": (self.decision or {}).get("audit_record_id"),
            "payment_id": (self.payment or {}).get("payment_id"),
            "consent_request_id": self._consent_request_id(),
            "dispute_id": (self.dispute or {}).get("dispute_id"),
        }
        found = known.get(name)
        if not found:
            raise StepFailed(where, f"the run has no {name} yet")
        return str(found)

    def _fill(self, route: str) -> tuple[str, str]:
        """`POST /v1/mandates/{mandate_id}/revoke` -> ("POST", "/v1/mandates/man_.../revoke")."""
        method, path = route.split(" ", 1)
        return method, _PLACEHOLDER.sub(lambda m: self._known(m.group(1), route), path)

    def _filled_body(self, step: TenantStep) -> Json | None:
        """The step's body, each `{name}` value filled from the run, and `respond_by` set
        `respond_in_days` from now."""
        if step.body is None and step.respond_in_days is None:
            return None
        body: Json = {}
        for name, value in (step.body or {}).items():
            whole = _PLACEHOLDER.fullmatch(value) if isinstance(value, str) else None
            body[name] = self._known(whole.group(1), step.route) if whole else value
        if step.respond_in_days is not None:
            body["respond_by"] = _iso(self._now() + timedelta(days=step.respond_in_days))
        return body

    def _consent_request_id(self) -> str | None:
        step_up = (self.decision or {}).get("step_up") or {}
        found = step_up.get("consent_request_id")
        return str(found) if found else None

    def _remember(self, body: Json) -> DecisionView | None:
        """Keep what the run needs from an answer; the Decision's view if it is one."""
        if "outcome" in body:
            self.decision = body
            return _decision_view(body)
        if "intent_id" in body:
            self.intent_id = str(body["intent_id"])
        elif "versions" in body and "chain" in body:
            self.pack_check = body
        elif "dispute_id" in body and "reason" in body:
            self.dispute = body
        elif "identical" in body:
            self.replayed = body
        elif "clean" in body:
            self.chain = body
        elif "payment_id" in body:
            self.payment = body
        elif "consent_request_id" in body and "status" in body:
            self.consent = body
        return None

    async def _with_rules(self, view: DecisionView | None) -> DecisionView | None:
        """The Decision's view with its Rule results, read once from its Audit record."""
        if view is None:
            return None
        audit_record_id = view.audit_record_id
        if audit_record_id not in self._rules:
            path = f"/v1/audit-records/{audit_record_id}"
            try:
                response = await self._tenant.get(path)
            except httpx.HTTPError:
                return view
            if not response.is_success:
                return view
            rules = _body(response).get("rule_results", [])
            self._rules[audit_record_id] = [RuleView.model_validate(rule) for rule in rules]
        return view.model_copy(update={"rule_results": self._rules[audit_record_id]})

    async def verify(self) -> Verification | None:
        """Replay this run's Decision and walk the Tenant's Audit chain, or None before
        the run has a Decision. Reads only, so it can't change the run."""
        if self.decision is None:
            return None
        step = "Verify / Replay"
        pack = None
        if self.dispute is not None:
            path = f"/v1/disputes/{self.dispute['dispute_id']}/verify-pack"
            checked = await self._send(self._tenant, step, "GET", path)
            if not checked.is_success:
                raise StepFailed(step, _problem(checked))
            pack = PackCheckView.model_validate(_body(checked))
        audit_record_id = self.decision["audit_record_id"]
        replayed = await self._send(
            self._tenant, step, "GET", f"/v1/audit-records/{audit_record_id}/replay"
        )
        if not replayed.is_success:
            raise StepFailed(step, _problem(replayed))
        chain = await self._send(self._tenant, step, "GET", "/v1/audit-chain/verify")
        if not chain.is_success:
            raise StepFailed(step, _problem(chain))
        return Verification(
            replay=ReplayView.model_validate(_body(replayed)),
            chain=ChainView.model_validate(_body(chain)),
            pack=pack,
        )

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
        # Valid from just now, or (B10) granted for a period that already ended, or (B9)
        # for one that starts later.
        until = now + timedelta(days=terms.valid_for_days)
        since = now - timedelta(minutes=5)
        if terms.expired_days_ago is not None:
            until = now - timedelta(days=terms.expired_days_ago)
            since = until - timedelta(days=terms.valid_for_days)
        if terms.starts_in_days is not None:
            since = now + timedelta(days=terms.starts_in_days)
            until = since + timedelta(days=terms.valid_for_days)
        # Asha agreed when the Mandate was granted, which is never in the future.
        agreed = min(since, now - timedelta(minutes=5)) - timedelta(minutes=1)
        mandate_body: Json = {
            "payer_id": payer["payer_id"],
            "allowed_agent_ids": [agent["agent_id"]],
            "currency": terms.per_txn_cap.currency,
            "per_txn_cap": terms.per_txn_cap.model_dump(),
            "monthly_cap": terms.monthly_cap.model_dump(),
            "allowed_mccs": terms.allowed_mccs,
            "valid_from": _iso(since),
            "expires_at": _iso(until),
            "consent_evidence": {
                "method": "in_app_confirm",
                "reference": "demo-consent",
                "captured_at": _iso(agreed),
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
        for i, prior in enumerate(setup.history):
            await self._pay_before(i, prior.amount, prior.merchant or setup.merchant, key)
        return Card(
            step=0,
            actor="tenant",
            chat=[],
            call=Call(
                kind="route", name="POST /v1/mandates", request=mandate_body, response=mandate
            ),
        )

    async def _pay_before(self, i: int, amount: Money, merchant: Merchant, key: str) -> None:
        """One Payment of the setup's history: ShopBot asks and pays through the same tools
        as in a step, so it counts in Spent this month exactly as a real one would."""
        step = "Earlier payments"
        asked = {
            "mandate_id": self.mandate_id,
            "agent_id": self.agent_id,
            "amount": amount.model_dump(),
            "merchant": merchant.model_dump(),
            "idempotency_key": f"{key}-history-{i}",
        }
        decided = await call(self._agent, _TOOLS["request_decision"], asked)
        decision = dict(decided.structured_content or {})
        if decided.is_error or decision.get("outcome") != "allow":
            got = decision.get("code") or decision.get("outcome")
            raise StepFailed(step, f"earlier payment {i + 1} wasn't allowed ({got})")
        paid = await call(
            self._agent,
            _TOOLS["create_payment"],
            {"decision_id": decision["decision_id"], "idempotency_key": f"{key}-history-{i}-pay"},
        )
        if paid.is_error:
            code = (paid.structured_content or {}).get("code", "TOOL_ERROR")
            raise StepFailed(step, f"earlier payment {i + 1} couldn't be paid ({code})")

    # --- Agent tool steps -----------------------------------------------------------------

    def _acting(self, step: AgentStep) -> str | None:
        """The Agent the step asks as: ShopBot, or another Agent the run registered."""
        if step.as_agent is None:
            return self.agent_id
        if step.as_agent not in self._agents:
            raise StepFailed(str(step.tool), f"no Agent {step.as_agent} was registered")
        return self._agents[step.as_agent]

    def _arguments(self, index: int, step: AgentStep) -> Json:
        tool = step.tool
        if tool == "request_decision":
            if step.amount is None:
                raise StepFailed(tool, "the step names no amount")
            asked: Json = {
                "mandate_id": self.mandate_id,
                "agent_id": self._acting(step),
                "amount": step.amount.model_dump(),
                "merchant": self.scenario.setup.merchant.model_dump(),
                "idempotency_key": self._key(index),
            }
            if self.intent_id is not None:
                asked["intent_id"] = self.intent_id
            return asked
        if tool == "check_intent":
            if step.amount is None or step.instruction is None or step.item is None:
                raise StepFailed(tool, "the step needs an amount, an instruction and an item")
            total = step.amount.model_dump()
            return {
                "mandate_id": self.mandate_id,
                "agent_id": self._acting(step),
                "payer_instruction": step.instruction,
                "merchant": self.scenario.setup.merchant.model_dump(),
                "line_items": [{"description": step.item, "quantity": 1, "unit_amount": total}],
                "quoted_total": total,
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
        decision = await self._with_rules(self._remember(body) if refused is None else None)
        intent = None
        if tool == "check_intent" and refused is None:
            intent = await self._intent_view(index, str(body["intent_id"]))
        waiting = step.until_status is not None and body.get("status") not in step.until_status
        request = {k: v for k, v in arguments.items() if k != "idempotency_key"}
        return Card(
            step=index,
            actor="agent",
            chat=step.chat,
            call=Call(kind="tool", name=tool, request=request, response=body),
            decision=decision,
            intent=intent,
            consent=_consent_view(body) if "outcome" not in body else None,
            refused=refused,
            waiting=waiting,
        )

    async def _intent_view(self, index: int, intent_id: str) -> IntentView | None:
        """The cart check's verdict, beside the one the Scenario scripted. A live verdict
        that differs is flagged, and the run carries on with it; a failed read leaves the
        card without a verdict, as the Decision stands either way."""
        try:
            response = await self._tenant.get(f"/v1/intents/{intent_id}")
        except httpx.HTTPError:
            return None
        if not response.is_success:
            return None
        check = _body(response)
        verdict = str(check["verdict"])
        scripted = self.scenario.scripted_verdicts().get(index - 1)
        differs = scripted is not None and verdict != scripted
        if differs:
            self._problems.append(
                f"cart check: the live model said {verdict}, the script says {scripted}"
            )
        return IntentView(
            intent_id=intent_id,
            verdict=verdict,
            failure_cause=check.get("failure_cause"),
            scripted=scripted,
            differs=differs,
        )

    # --- Route steps: the wallet app, or ShopBot calling a route no tool reaches -----------

    async def _route_step(self, index: int, step: AgentStep | TenantStep) -> Card:
        route = str(step.route)
        method, path = self._fill(route)
        body = self._filled_body(step) if isinstance(step, TenantStep) else None
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
        if route == REGISTER_AGENT and refused is None:
            self._agents[str(answer["name"])] = str(answer["agent_id"])
        decision = await self._with_rules(self._remember(answer) if refused is None else None)
        waiting = (
            refused is None
            and isinstance(step, TenantStep)
            and step.until_status is not None
            and answer.get("status") not in step.until_status
        )
        return Card(
            step=index,
            actor=step.actor,
            chat=step.chat,
            call=Call(kind="route", name=route, request=body, response=answer),
            decision=decision,
            dispute=_dispute_view(answer) if refused is None else None,
            pack_check=_pack_check_view(answer) if refused is None else None,
            refused=refused,
            waiting=waiting,
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

    async def _read_consent(self, step: str) -> tuple[str, Json]:
        """The Consent request as the Decision read shows it: status and, for a blocked
        approval, the re-check's Reason codes."""
        consent_request_id = self._consent_request_id()
        decision_id = (self.decision or {}).get("decision_id")
        path = f"/v1/decisions/{decision_id}"
        response = await self._send(self._tenant, step, "GET", path)
        if not response.is_success:
            raise StepFailed(step, _problem(response))
        consent = {
            **(_body(response).get("consent") or {}),
            "consent_request_id": consent_request_id,
        }
        self.consent = consent
        return f"GET {path}", consent

    async def _payer_step(self, index: int, step: HumanStep) -> Card:
        if step.action == "ignore":
            # Asha has her link but never opens it: the request stays pending.
            name, consent = await self._read_consent("Asha's phone")
            return Card(
                step=index,
                actor="payer",
                chat=step.chat,
                call=Call(kind="route", name=name, request=None, response=consent),
                consent=_consent_view(consent),
            )
        _, link = await self._link(index)
        if self.people == "engine":
            return await self._tap(index, step, link)
        name, consent = await self._read_consent("Asha's answer")
        return Card(
            step=index,
            actor="payer",
            chat=step.chat,
            call=Call(kind="route", name=name, request=None, response=consent),
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

    # --- Reviewer steps: the dispute viewer, on a Review link ------------------------------

    async def _read_dispute(self, step: str) -> Json:
        dispute_id = self._known("dispute_id", step)
        response = await self._send(self._tenant, step, "GET", f"/v1/disputes/{dispute_id}")
        if not response.is_success:
            raise StepFailed(step, _problem(response))
        self.dispute = _body(response)
        return self.dispute

    async def _review_url(self, index: int) -> str:
        """The run's Review link, minted on its first reviewer step."""
        if self._review_link is None:
            dispute_id = self._known("dispute_id", "Make a Review link")
            minted = await self._post(
                "Make a Review link",
                f"/v1/disputes/{dispute_id}/review-link",
                {"reviewer": _REVIEWER},
                f"{self._key(index)}-review-link",
            )
            if not minted.get("url"):
                raise StepFailed("Make a Review link", "the link's answer was lost; start again")
            self._review_link = str(minted["url"])
        return self._review_link

    async def _reviewer_step(self, index: int, step: HumanStep) -> Card:
        """The reviewer signs off or regenerates the pack in the dispute viewer. When a
        visitor plays the reviewer, the step waits while the pack is still in review: the
        Dispute is then signed off (ready), rebuilding (open), or closed by the Tenant."""
        link = await self._review_url(index)
        if self.people == "engine":
            return await self._review(index, step, link)
        name = "The dispute viewer"
        dispute = await self._read_dispute(name)
        return Card(
            step=index,
            actor="reviewer",
            chat=step.chat,
            call=Call(
                kind="route",
                name=f"GET /v1/disputes/{dispute['dispute_id']}",
                request=None,
                response=dispute,
            ),
            link=link,
            dispute=_dispute_view(dispute),
            waiting=dispute.get("status") == "in_review",
        )

    async def _review(self, index: int, step: HumanStep, link: str) -> Card:
        """CI's reviewer: acts on the Review link as the dispute viewer does, on the
        latest pack version."""
        name = "The reviewer's answer"
        latest = ((await self._read_dispute(name)).get("pack") or {}).get("version")
        action = "sign-off" if step.action == "sign_off" else "regenerate"
        body = {"pack_version": latest} if action == "sign-off" else None
        headers = {
            "Review-Link": link.split("#", 1)[1],
            "Idempotency-Key": f"{self._key(index)}-review",
        }
        route = f"/v1/review-link/{action}"
        response = await self._send(self._tenant, name, "POST", route, body, headers)
        if not response.is_success:
            raise StepFailed(name, _problem(response))
        answer = _body(response)
        self._remember(answer)
        return Card(
            step=index,
            actor="reviewer",
            chat=step.chat,
            call=Call(kind="route", name=f"POST {route}", request=body, response=answer),
            link=link,
            dispute=_dispute_view(answer),
        )

    # --- Fast-forward: a Demo control the visitor presses (ADR-0032) ----------------------

    async def _fast_forward(self, index: int, step: VisitorStep, press: bool) -> Card:
        if self.people == "visitor" and not press:
            return Card(
                step=index,
                actor="visitor",
                chat=step.chat,
                call=None,
                fast_forward=step.label,
                switch=not step.control.startswith("expire_"),
                waiting=True,
            )
        route = _CONTROL_ROUTES[step.control]
        method, path = self._fill(route)
        headers = {"Idempotency-Key": self._key(index)} if method == "POST" else None
        response = await self._send(self._tenant, step.label, method, path, None, headers)
        if not response.is_success:
            raise StepFailed(step.label, _problem(response))
        answer = _body(response)
        if step.control == "tamper_copy":
            self.chain = answer
        consent = None
        if step.control == "expire_consent":
            _, read = await self._read_consent(step.label)
            consent = _consent_view(read)
        return Card(
            step=index,
            actor="visitor",
            chat=step.chat,
            call=Call(kind="route", name=f"{method} {path}", request=None, response=answer),
            consent=consent,
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
        check(
            "payment failure code",
            expected.payment_failure_code,
            (self.payment or {}).get("failure_code"),
        )
        check("consent status", expected.consent_status, (self.consent or {}).get("status"))
        check("refusal", expected.refusal_code, self.refusal)
        check("replay", expected.replay_status, (self.replayed or {}).get("status"))
        check("chain clean", expected.chain_clean, (self.chain or {}).get("clean"))
        dispute = self.dispute or {}
        pack = dispute.get("pack") or {}
        check("dispute status", expected.dispute_status, dispute.get("status"))
        check(
            "evidence strength",
            expected.evidence_strength,
            (pack.get("assessment") or {}).get("strength"),
        )
        check("narrative source", expected.narrative_source, pack.get("narrative_source"))
        check("pack clean", expected.pack_clean, (self.pack_check or {}).get("clean"))
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


def _dispute_view(body: Json) -> DisputeView | None:
    if "dispute_id" not in body or "reason" not in body:
        return None
    pack = body.get("pack") or {}
    sign_off = body.get("sign_off") or {}
    return DisputeView(
        dispute_id=str(body["dispute_id"]),
        status=str(body["status"]),
        pack_version=pack.get("version"),
        pack_hash=body.get("pack_hash"),
        evidence_strength=(pack.get("assessment") or {}).get("strength"),
        narrative_source=pack.get("narrative_source"),
        narrative_note=pack.get("narrative_note"),
        signed_off_version=sign_off.get("pack_version"),
        reviewer=(sign_off.get("reviewer") or {}).get("name"),
    )


def _pack_check_view(body: Json) -> PackCheckView | None:
    if "versions" not in body or "chain" not in body:
        return None
    return PackCheckView.model_validate(body)


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
        if card.fast_forward:  # nobody to press it here: press it at once
            card = await run.next(press=True)
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
