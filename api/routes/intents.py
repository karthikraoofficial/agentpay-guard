"""/v1/intents: an Agent submits the Payer instruction and its cart for an Intent check
before asking for a Decision.

The response is only `{intent_id, expires_at}`. The verdict is never shown to the Agent
here, so it can't reword a cart until it gets a match; the Tenant reads it with GET.
Mandate status and Agent eligibility are not checked: the Decision enforces those. The
model call runs inside the write, while the Idempotency-Key is held, so a retry returns
the stored check without calling the model again.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict

from api.deps import ClockDep, GatewayDep, IdemKey, PackDep, SettingsDep, StoreDep, Tenant
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from core.domain.decisions import LineItem, Merchant
from core.domain.intents import (
    FailureCause,
    IntentCheck,
    IntentContent,
    IntentId,
    IntentVerdict,
    PayerInstruction,
)
from core.domain.mandates import MandateId
from core.domain.money import Money
from core.domain.parties import AgentId, new_id
from core.signals.intent_check import PROMPT_VERSION, run_intent_check

router = APIRouter(prefix="/v1/intents", tags=["intents"])

# Used when the active Policy pack sets no `intent` section (packs before 1.1.0); the
# same value 1.1.0 sets.
DEFAULT_VERDICT_TTL = timedelta(minutes=30)


class IntentCreate(IntentContent):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mandate_id: MandateId
    agent_id: AgentId

    def content(self) -> IntentContent:
        return IntentContent.model_validate(self.model_dump(exclude={"mandate_id", "agent_id"}))


class IntentCreated(BaseModel):
    intent_id: IntentId
    expires_at: AwareDatetime


class IntentOut(BaseModel):
    """For the Tenant only: the stored check, with its verdict."""

    intent_id: IntentId
    mandate_id: MandateId
    agent_id: AgentId
    payer_instruction: PayerInstruction
    merchant: Merchant
    line_items: tuple[LineItem, ...]
    quoted_total: Money
    content_hash: str
    verdict: IntentVerdict
    failure_cause: FailureCause | None
    model: str
    prompt_version: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd_micros: int | None
    latency_ms: int | None
    created_at: AwareDatetime
    expires_at: AwareDatetime


@router.post("", status_code=201, response_model=IntentCreated)
def create_intent(
    request: Request,
    body: IntentCreate,
    tenant: Tenant,
    store: StoreDep,
    clock: ClockDep,
    pack: PackDep,
    gateway: GatewayDep,
    settings: SettingsDep,
    key: IdemKey,
) -> Response:
    def write(now: datetime) -> tuple[int, IntentCreated]:
        mandate = tx.get_mandate(body.mandate_id)
        if mandate is None:
            raise ApiError(404, "MANDATE_NOT_FOUND", f"No Mandate {body.mandate_id}.")
        if tx.get_agent(body.agent_id) is None:
            raise ApiError(404, "AGENT_NOT_FOUND", f"No Agent {body.agent_id}.")
        content = body.content()
        if content.quoted_total.currency != mandate.currency:
            raise ApiError(
                422,
                "VALIDATION_FAILED",
                f"Amounts must be in the Mandate's currency, {mandate.currency}.",
            )
        result = run_intent_check(
            gateway,
            tenant,
            content,
            enabled=settings.intent_check_enabled,
            model=settings.intent_check_model,
            timeout_ms=settings.intent_check_timeout_ms,
        )
        params = pack.pack.intent
        ttl = (
            DEFAULT_VERDICT_TTL if params is None else timedelta(seconds=params.verdict_ttl_seconds)
        )
        completion = result.completion
        check = IntentCheck(
            intent_id=IntentId(new_id("int")),
            tenant_id=tenant,
            mandate_id=mandate.mandate_id,
            agent_id=body.agent_id,
            content=content,
            content_hash=content.content_hash(),
            verdict=result.verdict,
            failure_cause=result.failure_cause,
            model=settings.intent_check_model,
            prompt_version=PROMPT_VERSION,
            tokens_in=completion.tokens_in if completion else None,
            tokens_out=completion.tokens_out if completion else None,
            cost_usd_micros=completion.cost_usd_micros if completion else None,
            latency_ms=completion.latency_ms if completion else None,
            created_at=now,
            expires_at=now + ttl,
        )
        tx.add_intent_check(check)
        return 201, IntentCreated(intent_id=check.intent_id, expires_at=check.expires_at)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, body), clock, write)


@router.get("/{intent_id}")
def get_intent(intent_id: str, tenant: Tenant, store: StoreDep) -> IntentOut:
    with store.transaction(tenant) as tx:
        check = tx.get_intent_check(IntentId(intent_id))
    if check is None:
        raise ApiError(404, "INTENT_NOT_FOUND", f"No Intent check {intent_id}.")
    fields = check.model_dump(exclude={"tenant_id", "content"})
    return IntentOut.model_validate({**fields, **check.content.model_dump()})
