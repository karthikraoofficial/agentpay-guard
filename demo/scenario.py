"""The Scenario file format (#273): one JSON file per demo journey in the Scenario
catalogue (`demo/scenarios/`), keyed by the journey ids in `docs/demo-journeys.md`.

This model is the one source of the format. `demo/scenario.schema.json` is generated from
it (`uv run python -m demo.scenario --write-schema`), and a test fails when the committed
schema is stale, so the web app, `python -m demo` and the Demo runner read one format.

The fields follow `docs/demo-journeys.md` section 8. The chat script is carried on the
steps (each step's `chat` lines come before its call), and the human steps (approve,
decline, ignore, sign off, regenerate) are steps of their own, so the order of what is
said and done is the order of one list. Withdrawing a Dispute is the wallet app's call, a
Tenant step, as the API has it (G10).

Each step names its actor. An Agent step names a `guard_mcp` tool and goes through that
tool layer with the decide key; a Tenant step names an HTTP route; a Payer or reviewer
step names what the person does, on the Consent link or Review link the run was given.

Money is integer minor units and an ISO currency (CLAUDE.md non-negotiable 8).
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from core.domain.decisions import Merchant
from core.domain.intents import IntentVerdict
from core.domain.money import Money

CATALOGUE = Path(__file__).parent / "scenarios"
SCHEMA = Path(__file__).parent / "scenario.schema.json"

ScenarioId = Annotated[str, StringConstraints(pattern=r"^[A-H][0-9]{1,2}$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
# The tools guard_mcp publishes (guard_mcp/tools.py); a test checks the two lists agree.
AgentTool = Literal[
    "request_decision",
    "check_intent",
    "get_decision",
    "get_consent_request",
    "create_payment",
    "get_payment",
]
# The Demo controls in the Demo Studio PRD (#272): expire-now, settle-now and switches.
DemoControl = Literal[
    "expire_consent",
    "expire_payment_window",
    "expire_intent",
    "expire_notice",
    "expire_dispute",
    "expire_mandate",
    "settle_now",
    "ai_off",
    "rail_declining",
    "lose_payment",
    "tamper_copy",
    "rail_down",
]
# The mock rail's test tokens (adapters/memory/rail.py), each a pretend-bank behaviour.
MockCard = Literal[
    "token_mock_captures",
    "token_mock_fails",
    "token_mock_refused",
    "token_mock_times_out",
    "token_mock_notice_fails",
]
Playlist = Literal["pitch", "payments-pm", "risk-compliance", "engineering", "disputes"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChatLine(_Model):
    # Asha, ShopBot, or (B6) DealHunter, an Agent Asha never authorised.
    speaker: Literal["asha", "shopbot", "dealhunter"]
    text: Text


class MandateTerms(_Model):
    """What Asha grants ShopBot. Each run grants its own Payer, Agent and Mandate."""

    per_txn_cap: Money
    monthly_cap: Money
    allowed_mccs: list[Annotated[str, StringConstraints(pattern=r"^[0-9]{4}$")]]
    # How long the Mandate has been valid, and how long it still is.
    valid_for_days: int = Field(default=30, ge=1, le=365)
    # Granted already expired: its validity ended this many days ago (B10). The real rule
    # then gives MANDATE_EXPIRED, with no Demo control and nothing edited (ADR-0032).
    expired_days_ago: int | None = Field(default=None, ge=1, le=300)
    # Granted to start later: its validity begins this many days from now (B9). The real
    # rule then gives MANDATE_NOT_YET_VALID, again with nothing edited.
    starts_in_days: int | None = Field(default=None, ge=1, le=300)

    @model_validator(mode="after")
    def _one_window(self) -> Self:
        if self.expired_days_ago is not None and self.starts_in_days is not None:
            raise ValueError("a Mandate can't both start later and have expired already")
        return self


class PriorPayment(_Model):
    """History built before the steps, e.g. for Spent this month: ShopBot asks for this
    amount, is allowed, and pays. The setup doesn't wait for the Payment to be captured,
    since an allow counts in Spent this month from the moment it is made (B2)."""

    amount: Money
    merchant: Merchant | None = None


class Setup(_Model):
    mandate: MandateTerms
    merchant: Merchant
    card: MockCard = "token_mock_captures"
    history: list[PriorPayment] = []


# An HTTP route as `METHOD /v1/path`; `{name}` is filled from the run: mandate_id,
# agent_id, decision_id, audit_record_id, payment_id, consent_request_id or dispute_id.
Route = Annotated[str, StringConstraints(pattern=r"^(GET|POST) /v1/\S+$")]
# The Tenant step that registers another Agent, by the `name` in its body (B6).
REGISTER_AGENT = "POST /v1/agents"


class AgentStep(_Model):
    """ShopBot calls a guard_mcp tool. The run fills in what it already knows (Mandate,
    Agent, merchant, the last Decision or Payment, the step's idempotency key).

    `route` instead of `tool` has ShopBot call an HTTP route with its decide key, for
    journeys that show a call no tool makes, e.g. trying to approve its own step_up."""

    actor: Literal["agent"]
    tool: AgentTool | None = None
    route: Route | None = None
    # request_decision or check_intent as another Agent than ShopBot: the name an earlier
    # `POST /v1/agents` Tenant step registered (B6).
    as_agent: Text | None = None
    chat: list[ChatLine] = []
    # request_decision: the amount to ask for. check_intent: the cart's Quoted total.
    amount: Money | None = None
    # check_intent: the Payer instruction, and the cart's one line item.
    instruction: Text | None = None
    item: Text | None = None
    # A read the run repeats until the answer's `status` is one of these (the worker or a
    # person has to act first). The step waits while it isn't.
    until_status: list[str] | None = None
    # The error code the API answers with, when the journey shows a refusal.
    expect_refusal: str | None = None
    # How many times ShopBot sends the call, each with the step's one idempotency key, as
    # a retry after a lost answer would (F3). Every send must get the first answer back.
    sends: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def _tool_or_route(self) -> Self:
        if (self.tool is None) == (self.route is None):
            raise ValueError("an Agent step names exactly one of tool or route")
        return self

    @model_validator(mode="after")
    def _only_writes_resend(self) -> Self:
        if self.sends > 1 and self.tool not in KEYED_TOOLS:
            raise ValueError(
                f"only a tool that takes an idempotency key is resent: {sorted(KEYED_TOOLS)}"
            )
        return self


# The guard_mcp tools that write, each taking an idempotency key.
KEYED_TOOLS = frozenset({"request_decision", "check_intent", "create_payment"})


class TenantStep(_Model):
    """The wallet app calls an HTTP route with the tenant key, e.g.
    `POST /v1/mandates/{mandate_id}/revoke`.

    A body value that is exactly `{name}` is filled from the run as in a route, e.g.
    `{"payment_id": "{payment_id}"}` to open a Dispute on the run's Payment."""

    actor: Literal["tenant"]
    route: Route
    chat: list[ChatLine] = []
    body: dict[str, Any] | None = None
    # Sets the body's `respond_by` to this many days from now: a Dispute's deadline.
    respond_in_days: int | None = Field(default=None, ge=1, le=120)
    # A read the run repeats until the answer's `status` is one of these, e.g. a Dispute
    # waiting for its Evidence pack (`in_review`).
    until_status: list[str] | None = None
    expect_refusal: str | None = None

    @model_validator(mode="after")
    def _only_reads_wait(self) -> Self:
        if self.until_status is not None and not self.route.startswith("GET "):
            raise ValueError("only a GET route can wait with until_status")
        return self


class HumanStep(_Model):
    """A person acts: Asha on her Consent link, or the reviewer on a Review link.

    The run mints the link. When a visitor plays Asha, the step waits until she has
    answered on the consent screen; in CI the engine answers for her, sending each of
    `taps` with one idempotency key, as a double tap on a slow connection would."""

    actor: Literal["payer", "reviewer"]
    action: Literal["approve", "decline", "ignore", "sign_off", "regenerate"]
    chat: list[ChatLine] = []
    taps: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def _actions_are_the_actors(self) -> Self:
        allowed = REVIEWER_ACTIONS if self.actor == "reviewer" else PAYER_ACTIONS
        if self.action not in allowed:
            raise ValueError(f"a {self.actor} step's action is one of {sorted(allowed)}")
        return self


# What each person can do on their link: the consent screen, and the dispute viewer.
PAYER_ACTIONS = frozenset({"approve", "decline", "ignore"})
REVIEWER_ACTIONS = frozenset({"sign_off", "regenerate"})


# The Demo controls a visitor can press: fast-forwards (#280) and switches (#281).
FastForward = Literal[
    "expire_consent",
    "expire_payment_window",
    "expire_notice",
    "expire_dispute",
    "ai_off",
    "rail_declining",
    "lose_payment",
    "rail_down",
    "tamper_copy",
]


class VisitorStep(_Model):
    """The visitor presses a Demo control: a fast-forward ends a timer now, a switch forces
    a failure (ADR-0032). On the demo page the run waits for the press; the CLI and CI
    press it at once."""

    actor: Literal["visitor"]
    control: FastForward
    # The button's words, e.g. "Skip 10 minutes".
    label: Text
    chat: list[ChatLine] = []


Step = Annotated[AgentStep | TenantStep | HumanStep | VisitorStep, Field(discriminator="actor")]


class Expected(_Model):
    """What the run must end with; CI checks each field that is set."""

    outcome: Literal["allow", "step_up", "block"] | None = None
    reason_codes: list[str] | None = None
    # The error code of an HTTP refusal the journey shows, e.g. DECISION_ALREADY_PAID.
    refusal_code: str | None = None
    payment_status: Literal["scheduled", "captured", "failed"] | None = None
    refund_status: Literal["pending", "processed", "failed"] | None = None
    dispute_status: str | None = None
    # The latest Evidence pack's assessment, as the Dispute golden cases name it: its
    # strength, the Visa and Mastercard reason codes, and the Evidence facts missing for a
    # strong pack (an empty list when none are).
    evidence_strength: Literal["strong", "mixed", "weak"] | None = None
    visa_code: str | None = None
    mastercard_code: str | None = None
    missing_evidence: list[str] | None = None
    # The last Payment's failure code, when it failed, e.g. MANDATE_REVOKED.
    payment_failure_code: str | None = None
    # The last Consent request's status at the end of the run.
    consent_status: Literal["pending", "approved", "declined", "expired", "blocked"] | None = None
    # What a Replay step answered for the Decision (H4).
    replay_status: Literal["identical", "different"] | None = None
    # Whether a Verify step found the Tenant's Audit chain clean (H5).
    chain_clean: bool | None = None
    # Who wrote the latest Evidence pack's narrative: the model, or code (G8).
    narrative_source: Literal["model", "template"] | None = None
    # Whether a verify-pack step found every pack version matching the Audit chain and
    # its Sign-off, and the chain clean (H7).
    pack_clean: bool | None = None


class ModelReply(_Model):
    """A scripted model reply CI plays through the fake model gateway, so CI makes no paid
    call. The live demo compares the live verdict with it.

    An Intent check's reply is `{"verdict": "match" | "mismatch" | "unknown"}`: the one
    word the model answers (core/signals/intent_check.py)."""

    job: Literal["intent_check", "dispute_narrative"]
    reply: dict[str, Any]

    @model_validator(mode="after")
    def _intent_reply_is_a_verdict(self) -> Self:
        if self.job == "intent_check" and (
            set(self.reply) != {"verdict"} or self.reply["verdict"] not in get_args(IntentVerdict)
        ):
            raise ValueError(
                'an intent_check reply is {"verdict": "match" | "mismatch" | "unknown"}'
            )
        return self


class Scenario(_Model):
    id: ScenarioId
    title: Text
    group: Literal["A", "B", "C", "D", "E", "F", "G", "H"]
    playlists: list[Playlist] = []
    story: Text
    setup: Setup
    steps: list[Step] = Field(min_length=1)
    demo_controls: list[DemoControl] = []
    expected: Expected
    model_replies: list[ModelReply] = []
    what_to_notice: Text

    @model_validator(mode="after")
    def _controls_listed(self) -> Self:
        pressed = {step.control for step in self.steps if isinstance(step, VisitorStep)}
        if missing := pressed - set(self.demo_controls):
            raise ValueError(f"steps press Demo controls not in demo_controls: {sorted(missing)}")
        return self

    @model_validator(mode="after")
    def _agents_registered_first(self) -> Self:
        registered: set[str] = set()
        for step in self.steps:
            if isinstance(step, TenantStep) and step.route == REGISTER_AGENT:
                registered.add(str((step.body or {}).get("name")))
            elif isinstance(step, AgentStep) and step.as_agent is not None:
                if step.as_agent not in registered:
                    raise ValueError(
                        f"a step acts as {step.as_agent}, but no earlier "
                        f"`{REGISTER_AGENT}` step registers an Agent of that name"
                    )
        return self

    def _model_checks(self) -> list[int]:
        """The `check_intent` steps that reach the model, by index in `steps`: every one
        not made after the visitor switched the AI off (D5)."""
        found, ai_off = [], False
        for i, step in enumerate(self.steps):
            if isinstance(step, VisitorStep) and step.control == "ai_off":
                ai_off = True
            elif isinstance(step, AgentStep) and step.tool == "check_intent" and not ai_off:
                found.append(i)
        return found

    @model_validator(mode="after")
    def _one_reply_per_model_check(self) -> Self:
        checks = len(self._model_checks())
        replies = sum(1 for reply in self.model_replies if reply.job == "intent_check")
        if checks != replies:
            raise ValueError(
                f"{checks} Intent check step(s) reach the model, but there are {replies} "
                "scripted intent_check replies: script one for each, in order"
            )
        return self

    def scripted_verdicts(self) -> dict[int, IntentVerdict]:
        """Each model-reaching `check_intent` step's scripted verdict, by index in `steps`."""
        verdicts = [r.reply["verdict"] for r in self.model_replies if r.job == "intent_check"]
        return dict(zip(self._model_checks(), verdicts, strict=True))


def load(path: Path) -> Scenario:
    return Scenario.model_validate_json(path.read_text(encoding="utf-8"))


def catalogue(root: Path = CATALOGUE) -> dict[str, Scenario]:
    """Every Scenario file, by id. A file's name must be its id."""
    found: dict[str, Scenario] = {}
    for path in sorted(root.glob("*.json")):
        scenario = load(path)
        if scenario.id != path.stem:
            raise ValueError(
                f"{path.name} holds Scenario {scenario.id}; name it {scenario.id}.json"
            )
        found[scenario.id] = scenario
    return found


def schema_text() -> str:
    return json.dumps(Scenario.model_json_schema(), indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m demo.scenario")
    parser.add_argument("--write-schema", action="store_true", help=f"rewrite {SCHEMA.name}")
    args = parser.parse_args(argv)
    if args.write_schema:
        SCHEMA.write_text(schema_text(), encoding="utf-8", newline="\n")
        print(f"Wrote {SCHEMA}")
        return 0
    for scenario_id in catalogue():
        print(scenario_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
