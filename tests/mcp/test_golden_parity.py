"""Golden parity (#249): every Decision and approval golden case, decided through the MCP
server's `request_decision` and through POST /v1/decisions, gives the same Outcome,
Reason codes, pack version and Pack hash, so the MCP surface can't drift from the API.

Each case is set up twice, in a fresh Tenant each time, through the API only, as its
golden file describes it, on the pack the file is pinned to:

- its Mandate, Agents and Payer; a revocation at `revoked_at`; a disabled Agent;
- Spent this month, from earlier Decisions on the Mandate (each step_up approved),
  outside the Velocity window;
- a Velocity count, from that many blocked Decisions inside the window;
- known merchant history, from an allowed Decision at the merchant under a second Mandate
  of the same Payer;
- the Intent check it cites, through `check_intent` on the MCP side and POST /v1/intents
  on the other, with the scripted model reply that gives its verdict;
- for an approval case, the step_up, then the approval out of band with the Tenant's key.

Then the harness checks the set-up worked: the Signals and Spent this month the API
recorded are the case's, and the answer is the case's label. A case it can't set up
fails with what differed. The only exceptions are ENGINE_ONLY: cases that give
decide() a Signal the API always works out, and that test asserts they still can't be
set up, so the list can't go stale.
"""

import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import pytest
from mcp import Client

from core.domain.decisions import AuditRecordId
from core.policy.pack import PolicyPack
from core.policy.packs import load_pack
from core.ports import ModelGatewayError, ModelSpendCapReached, ModelTimeout, ModelUnavailable
from evals.run_evals import GOLDEN, PACKS, Case, is_decision_file, load_cases
from tests.api.conftest import Api
from tests.api.test_consent_decline import ATTESTATION
from tests.conftest import make_settings
from tests.mcp.conftest import connect, key_of, payload, text
from tests.packs import released_packs

# Its own marker: ~2 minutes, too slow for the default run. CI runs it as its own step.
pytestmark = [pytest.mark.anyio, pytest.mark.parity]

# Both surfaces share one store, so a second store adds nothing to parity; the API on
# Postgres is tested everywhere else.
MEMORY_ONLY = pytest.mark.parametrize("api", ["memory"], indirect=True)

# Cases the harness can't set up through the API, and why. Each gives decide() a null
# Signal to test what the engine does when one wasn't worked out; the API works out every
# Signal its pack has a rule for, so it never sends null.
_NULL_SIGNAL = "engine-only: the API always works out {}, so it is never null"
ENGINE_ONLY = {
    "phase2/velocity-missing": _NULL_SIGNAL.format("velocity_count"),
    "phase2/merchant-missing-medium": _NULL_SIGNAL.format("merchant_is_new"),
    "phase2/intent-resolution-missing": _NULL_SIGNAL.format("intent_resolution"),
    "scenarios/velocity-missing-medium": _NULL_SIGNAL.format("velocity_count"),
    "scenarios/velocity-missing-low": _NULL_SIGNAL.format("velocity_count"),
}


def _golden() -> dict[str, Case]:
    return {
        f"{path.stem}/{case.id}": case
        for path in sorted(GOLDEN.glob("*.jsonl"))
        if is_decision_file(path)
        for case in load_cases(path)
    }


CASES = _golden()


class CannotSetUp(Exception):
    """The case's inputs couldn't be reproduced through the API."""


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _at(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


# --- the two surfaces ------------------------------------------------------------------


class Surface(Protocol):
    """Where the case's own Intent check, Decision and read-back go."""

    async def check_intent(self, body: dict[str, Any]) -> str: ...
    async def decide(self, body: dict[str, Any]) -> dict[str, Any]: ...
    async def read(self, decision_id: str) -> dict[str, Any]: ...


@dataclass
class Http:
    api: Api
    key: str
    _keys: "itertools.count[int]" = field(default_factory=itertools.count)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}", "Idempotency-Key": f"p-{next(self._keys)}"}

    def _ok(self, response: Any) -> dict[str, Any]:
        if response.status_code >= 300:
            raise CannotSetUp(f"the API answered {response.status_code}: {response.text}")
        body: dict[str, Any] = response.json()
        return body

    async def check_intent(self, body: dict[str, Any]) -> str:
        response = self.api.client.post("/v1/intents", headers=self._headers(), json=body)
        return str(self._ok(response)["intent_id"])

    async def decide(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._ok(self.api.client.post("/v1/decisions", headers=self._headers(), json=body))

    async def read(self, decision_id: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.key}"}
        return self._ok(self.api.client.get(f"/v1/decisions/{decision_id}", headers=headers))


@dataclass
class Mcp:
    client: Client
    _keys: "itertools.count[int]" = field(default_factory=itertools.count)

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.client.call_tool(tool, arguments)
        if result.is_error:
            raise CannotSetUp(f"{tool} failed: {text(result)}")
        return payload(result)

    async def check_intent(self, body: dict[str, Any]) -> str:
        key = {"idempotency_key": f"p-{next(self._keys)}"}
        return str((await self._call("check_intent", body | key))["intent_id"])

    async def decide(self, body: dict[str, Any]) -> dict[str, Any]:
        key = {"idempotency_key": f"p-{next(self._keys)}"}
        return await self._call("request_decision", body | key)

    async def read(self, decision_id: str) -> dict[str, Any]:
        return await self._call("get_decision", {"decision_id": decision_id})


# --- one Tenant, set up from a case ----------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """What the two surfaces must agree on."""

    outcome: str
    reason_codes: tuple[str, ...]
    pack_version: str
    pack_hash: str
    # Approval cases only: the Consent resolution and its re-check's Reason codes.
    resolution: str | None = None
    recheck_reason_codes: tuple[str, ...] | None = None


@dataclass
class World:
    """A fresh Tenant, with everything a case needs set up through the API."""

    api: Api
    case: Case
    pack: PolicyPack
    tenant: dict[str, str] = field(init=False)
    payer_id: str = field(init=False)
    name: str
    agents: dict[str, str] = field(default_factory=dict)
    mandate_id: str = field(init=False)
    first_agent: str = field(init=False)
    # The id the case's request cites, once its Intent check is made.
    cited: str | None = None
    decision: dict[str, Any] = field(default_factory=dict)
    _second_mandate: str | None = None

    def __post_init__(self) -> None:
        self.tenant = self.api.new_tenant(self.name)

    def decide_key(self) -> str:
        """A decide-only key: what the MCP server holds, and what the API side uses too."""
        return key_of(self.api.new_key(self.api.tenant_of(self.tenant), {"decide"}))

    def at(self, when: datetime) -> None:
        self.api.clock.at = when

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.api.post(path, self.tenant, body)
        if response.status_code >= 300:
            raise CannotSetUp(f"POST {path} answered {response.status_code}: {response.text}")
        result: dict[str, Any] = response.json()
        return result

    def agent(self, case_agent_id: str) -> str:
        if case_agent_id not in self.agents:
            created = self.post("/v1/agents", {"name": f"Agent {case_agent_id}"})
            self.agents[case_agent_id] = created["agent_id"]
        return self.agents[case_agent_id]

    def mandate_body(self, **overrides: Any) -> dict[str, Any]:
        m = self.case.mandate
        return {
            "payer_id": self.payer_id,
            "allowed_agent_ids": [self.agent(a) for a in m["allowed_agent_ids"]],
            "currency": m["currency"],
            "per_txn_cap": m["per_txn_cap"],
            "monthly_cap": m["monthly_cap"],
            "allowed_mccs": m["allowed_mccs"],
            "valid_from": m["valid_from"],
            "expires_at": m["expires_at"],
            "consent_evidence": m["consent_evidence"],
        } | overrides

    def second_mandate(self) -> str:
        """Another Mandate of the same Payer, valid from setup time, for merchant history
        and for an Intent check on the wrong Mandate."""
        if self._second_mandate is None:
            start = self.api.clock.now()
            body = self.mandate_body(
                allowed_mccs=[_HISTORY_MCC],
                valid_from=_iso(start),
                expires_at=_iso(start + timedelta(days=30)),
            )
            self._second_mandate = self.post("/v1/mandates", body)["mandate_id"]
        return self._second_mandate

    def request(self, **overrides: Any) -> dict[str, Any]:
        """The case's Agent payment request, with this Tenant's ids."""
        body = dict(self.case.request) | {
            "mandate_id": self.mandate_id,
            "agent_id": self.request_agent,
        }
        body.pop("intent_id", None)
        return body | overrides

    @property
    def request_agent(self) -> str:
        case_agent = self.case.request["agent_id"]
        if self.case.agent_status is None:
            return f"agt_absent_{case_agent}"  # never created: the API finds no Agent
        return self.agent(case_agent)

    def prior(self, merchant: dict[str, str], minor: int, mandate_id: str = "") -> dict[str, Any]:
        """A Decision made to set the case up: plain, by the Mandate's first Agent."""
        body = {
            "mandate_id": mandate_id or self.mandate_id,
            "agent_id": self.first_agent,
            "amount": {"minor": minor, "currency": self.case.mandate["currency"]},
            "merchant": merchant,
        }
        return self.post("/v1/decisions", body)

    def approve(self, decision: dict[str, Any]) -> dict[str, Any]:
        csr = decision["step_up"]["consent_request_id"]
        return self.post(f"/v1/consent-requests/{csr}/approve", {"attestation": ATTESTATION})

    def audit(self, audit_record_id: str) -> Any:
        with self.api.store.transaction(self.api.tenant_of(self.tenant)) as tx:
            return tx.get_audit_record(AuditRecordId(audit_record_id)).record


# --- building the case's state ---------------------------------------------------------

_SPEND_MERCHANT = "m_parity_spend"
_BLOCKED_MCC = "0742"  # veterinary services: in no golden Mandate's allowed list
_HISTORY_MCC = "5411"  # groceries: low risk in every pack


def engine_only_reason(case: Case, pack: PolicyPack) -> str | None:
    rules = {
        "velocity_count": pack.velocity,
        "merchant_is_new": pack.merchant_new,
        "intent_resolution": pack.intent,
    }
    for name, rule in rules.items():
        if rule is not None and name in case.signals and case.signals[name] is None:
            return _NULL_SIGNAL.format(name)
    return None


def _window(pack: PolicyPack) -> timedelta:
    return timedelta(seconds=600 if pack.velocity is None else pack.velocity.window_seconds)


def _spend_priors(world: World, start: datetime) -> list[tuple[datetime, Callable[[], None]]]:
    """Earlier Decisions on the Mandate that add up to Spent this month, one second
    apart from `start`, each no larger than the Per-transaction cap."""
    case = world.case
    cap = case.mandate["per_txn_cap"]["minor"]
    left, chunks = case.spent_this_month_minor, []
    while left > 0:
        chunks.append(min(cap, left))
        left -= chunks[-1]

    def spend(minor: int) -> Callable[[], None]:
        def run() -> None:
            mcc = case.mandate["allowed_mccs"][0]
            merchant = {"id": _SPEND_MERCHANT, "name": "Parity spend", "mcc": mcc}
            prior = world.prior(merchant, minor)
            if prior["outcome"] == "step_up":
                if world.approve(prior)["status"] == "approved":
                    return
            elif prior["outcome"] == "allow":
                return
            raise CannotSetUp(f"a Decision for Spent this month wasn't allowed: {prior}")

        return run

    return [(start + timedelta(seconds=i), spend(m)) for i, m in enumerate(chunks)]


def _velocity_priors(world: World, before: datetime) -> list[tuple[datetime, Callable[[], None]]]:
    """That many blocked Decisions on the Mandate inside the Velocity window."""
    count = world.case.signals.get("velocity_count") or 0
    if not count:
        return []
    step = min(timedelta(seconds=1), _window(world.pack) / (count + 2))

    def blocked() -> None:
        merchant = {"id": "m_parity_velocity", "name": "Parity velocity", "mcc": _BLOCKED_MCC}
        prior = world.prior(merchant, 1)
        if prior["outcome"] != "block":
            raise CannotSetUp(f"a Decision for the Velocity count wasn't blocked: {prior}")

    return [(before - step * (count - i), blocked) for i in range(count)]


def _merchant_history(world: World) -> None:
    """An allowed Decision at the case's merchant under a second Mandate of the Payer.
    Merchant history is by merchant id, so it uses a low-risk MCC: no Decision at a
    high-risk one is ever allowed."""
    merchant = world.case.request["merchant"] | {"mcc": _HISTORY_MCC}
    mandate = world.second_mandate()
    world.at(world.api.clock.now() + timedelta(seconds=1))
    prior = world.prior(merchant, 1, mandate)
    if prior["outcome"] != "allow":
        raise CannotSetUp(f"the Decision giving merchant history wasn't allowed: {prior}")


def _reply(intent: dict[str, Any]) -> str | ModelGatewayError | None:
    """The scripted model reply that gives the case's Intent verdict."""
    cause = intent["failure_cause"]
    if cause == "disabled":
        return None  # the check is switched off in settings: no model call
    replies: dict[str, str | ModelGatewayError] = {
        "timeout": ModelTimeout("parity"),
        "error": ModelUnavailable("parity"),
        "capped": ModelSpendCapReached("parity"),
        "unparseable": "Probably fine.",
    }
    if cause is not None:
        return replies[cause]
    return {"match": "MATCH", "mismatch": "MISMATCH", "unknown": "UNKNOWN"}[intent["verdict"]]


def _intent_check(
    world: World, surface: Surface, decided_at: datetime
) -> tuple[datetime, Callable[[], Awaitable[None]]] | None:
    """The Intent check the case cites, made through `surface` at the right time, and the
    id to cite. Returns None when the case cites none."""
    signals, case = world.case.signals, world.case
    resolution = signals.get("intent_resolution")
    # What the request cites follows the Signals: rules never read `intent_id` itself, and
    # a base request may carry one that a none_cited case doesn't use.
    if resolution in (None, "none_cited"):
        if signals.get("cited_intent_id"):
            raise CannotSetUp(f"the case cites an Intent check but its resolution is {resolution}")
        return None
    if resolution == "not_found":
        world.cited = "int_parity_missing"
        return None
    intent = signals.get("intent") or {}
    merchant = dict(case.request["merchant"])
    agent = world.request_agent
    mandate = world.mandate_id
    total = intent.get("quoted_total", case.request["amount"])
    when = decided_at - timedelta(seconds=1)
    if resolution == "wrong_mandate":
        mandate = world.second_mandate()
    elif resolution == "wrong_agent":
        agent = world.agent("agt_parity_other")
    elif resolution == "merchant_mismatch":
        merchant["id"] = "m_parity_other"
    elif resolution == "expired":
        ttl = 1800 if world.pack.intent is None else world.pack.intent.verdict_ttl_seconds
        when = decided_at - timedelta(seconds=ttl + 60)
    elif resolution != "used":
        raise CannotSetUp(f"no way to set up intent_resolution {resolution}")
    body = {
        "mandate_id": mandate,
        "agent_id": agent,
        "payer_instruction": "Buy what is in my cart.",
        "merchant": merchant,
        "line_items": [{"description": "Cart", "quantity": 1, "unit_amount": total}],
        "quoted_total": total,
    }
    reply = _reply(intent) if resolution == "used" else "MATCH"

    async def run() -> None:
        if reply is not None:
            world.api.gateway.script.append(reply)
        world.cited = await surface.check_intent(body)

    return when, run


# --- running a case on one surface -----------------------------------------------------


async def run_case(world: World, surface: Surface) -> Answer:
    """Set the case up in `world`, decide it through `surface`, check the set-up gave
    the case's inputs and label, and return what the surface answered."""
    case = world.case
    if reason := engine_only_reason(case, world.pack):
        raise CannotSetUp(reason)
    now = case.now
    revoked_at = case.mandate.get("revoked_at")
    approval = case.stage == "approval"
    # An approval case's Decision comes first: before any revocation, and inside the
    # Consent request's life.
    decided_at = now
    if approval:
        decided_at = min(now, _at(revoked_at) if revoked_at else now) - timedelta(seconds=60)

    # The Tenant, its Payer and Agents, and the Mandate, when the case says it was made,
    # or a day before the Decision if that's earlier: no rule reads `created_at`, and
    # everything below happens within the day before the Decision.
    world.at(min(_at(case.mandate["created_at"]), decided_at - timedelta(days=1)))
    world.payer_id = world.post(
        "/v1/payers", {"display_name": "Parity", "contact_ref": "tok_parity"}
    )["payer_id"]
    world.mandate_id = world.post("/v1/mandates", world.mandate_body())["mandate_id"]
    world.first_agent = world.agent(case.mandate["allowed_agent_ids"][0])
    if case.agent_status is not None:
        world.agent(case.request["agent_id"])
    if case.signals.get("merchant_is_new") is False:
        _merchant_history(world)

    # Everything else, in time order: the Decisions behind Spent this month and the
    # Velocity count, the Intent check, a revocation, and a disabled Agent.
    events: list[tuple[datetime, Callable[[], Any]]] = []
    if approval:
        events += _spend_priors(world, decided_at + timedelta(seconds=1))
    else:
        events += _spend_priors(world, decided_at - _window(world.pack) - timedelta(minutes=10))
    events += _velocity_priors(world, decided_at)
    check = _intent_check(world, surface, decided_at)
    if check is not None:
        events.append(check)
    if revoked_at:
        by = case.mandate["revoked_by"]
        revoke = f"/v1/mandates/{world.mandate_id}/revoke"
        events.append((_at(revoked_at), lambda: world.post(revoke, {"revoked_by": by})))
    if case.agent_status == "disabled":
        events.append((now, lambda: world.post(f"/v1/agents/{world.request_agent}/disable")))

    async def decide() -> None:
        body = world.request()
        if world.cited is not None:
            body["intent_id"] = world.cited
        world.decision = await surface.decide(body)

    events.append((decided_at, decide))
    for when, run in sorted(events, key=lambda e: e[0]):  # stable: ties keep their order
        world.at(when)
        result = run()
        if isinstance(result, Awaitable):
            await result

    decision = world.decision
    _check_signals(world, world.audit(decision["audit_record_id"]).inputs)
    pack = decision["policy_pack"]
    answer = Answer(
        decision["outcome"], tuple(decision["reason_codes"]), pack["version"], pack["hash"]
    )
    label = case.expected
    if not approval:
        actual = (answer.outcome, answer.reason_codes)
        if actual != (label.outcome, label.reason_codes):
            raise CannotSetUp(f"the API decided {actual}, the label says {label}")
        return answer

    # The Payer approves out of band, with the Tenant's own key, at the case's time.
    if decision["outcome"] != "step_up":
        raise CannotSetUp(f"an approval case needs a step_up first, got {decision}")
    world.at(now)
    resolved = world.approve(decision)
    record = world.audit(resolved["audit_record_id"])
    _check_spent(world, record.inputs)
    recheck = (record.output.outcome, tuple(record.output.reason_codes), resolved["status"])
    if recheck != (label.outcome, label.reason_codes, label.resolution):
        raise CannotSetUp(f"the approval re-check gave {recheck}, the label says {label}")
    read = await surface.read(decision["decision_id"])
    return Answer(
        read["outcome"],
        tuple(read["reason_codes"]),
        read["policy_pack"]["version"],
        read["policy_pack"]["hash"],
        read["consent"]["status"],
        tuple(resolved["reason_codes"] or ()),
    )


def _check_spent(world: World, inputs: Any) -> None:
    """Spent this month as the engine saw it: at the Decision, or at an approval."""
    spent, want = inputs.spent_this_month.minor, world.case.spent_this_month_minor
    if spent != want:
        raise CannotSetUp(f"Spent this month is {spent}, the case has {want}")


def _check_signals(world: World, inputs: Any) -> None:
    """The Signals the API recorded with the Decision are the case's own (an approval
    re-check reuses them), and so is Spent this month on a Decision case."""
    case = world.case
    if case.stage == "decision":
        _check_spent(world, inputs)
    recorded = inputs.signals
    for name in ("velocity_count", "merchant_is_new", "intent_resolution"):
        if name in case.signals and getattr(recorded, name) != case.signals[name]:
            got, want = getattr(recorded, name), case.signals[name]
            raise CannotSetUp(f"{name} is {got!r}, the case has {want!r}")
    if "cited_intent_id" in case.signals:
        if (recorded.cited_intent_id is None) != (case.signals["cited_intent_id"] is None):
            raise CannotSetUp(f"cited_intent_id is {recorded.cited_intent_id!r}")
    if "intent" in case.signals:
        want = case.signals["intent"]
        got = recorded.intent
        if (got is None) != (want is None):
            raise CannotSetUp(f"the Intent signal is {got!r}, the case has {want!r}")
        if got is not None:
            mine = (got.verdict, got.failure_cause, got.quoted_total.model_dump())
            theirs = (want["verdict"], want["failure_cause"], want["quoted_total"])
            if mine != theirs:
                raise CannotSetUp(f"the Intent signal is {mine}, the case has {theirs}")


# --- the tests -------------------------------------------------------------------------


def _ready(api: Api, tmp_path: Path, case: Case) -> PolicyPack:
    api.use_packs(released_packs(tmp_path / "packs", case.pack.version))
    intent = case.signals.get("intent") or {}
    if intent.get("failure_cause") == "disabled":
        api.use_settings(make_settings(intent_check_enabled=False))
    return load_pack(PACKS, case.pack.id, case.pack.version).pack


@MEMORY_ONLY
@pytest.mark.parametrize("case_id", sorted(set(CASES) - set(ENGINE_ONLY)))
async def test_mcp_and_the_api_decide_each_golden_case_alike(
    api: Api, tmp_path: Path, case_id: str
) -> None:
    case = CASES[case_id]
    pack = _ready(api, tmp_path, case)

    world = World(api, case, pack, "parity-api")
    via_api = await run_case(world, Http(api, world.decide_key()))
    mcp_world = World(api, case, pack, "parity-mcp")
    async with connect(api, mcp_world.decide_key()) as client:
        via_mcp = await run_case(mcp_world, Mcp(client))

    assert via_mcp == via_api, case_id
    assert via_api.pack_version == case.pack.version


@MEMORY_ONLY
@pytest.mark.parametrize("case_id", sorted(ENGINE_ONLY))
async def test_an_engine_only_case_still_cannot_be_set_up_through_the_api(
    api: Api, tmp_path: Path, case_id: str
) -> None:
    case = CASES[case_id]  # a KeyError here means the case is gone: drop it from the list
    pack = _ready(api, tmp_path, case)

    with pytest.raises(CannotSetUp) as refused:
        await run_case(World(api, case, pack, "parity-api"), Http(api, "unused"))
    assert str(refused.value) == ENGINE_ONLY[case_id]


def test_every_case_the_api_cannot_set_up_is_listed_with_its_reason() -> None:
    reasons = {
        case_id: reason
        for case_id, case in CASES.items()
        if (
            reason := engine_only_reason(
                case, load_pack(PACKS, case.pack.id, case.pack.version).pack
            )
        )
    }
    assert reasons == ENGINE_ONLY
