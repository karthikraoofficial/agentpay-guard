"""/v1/decisions: an Agent asks whether it may pay, and gets allow, step_up or block.

Every Outcome is HTTP 200. One transaction locks the Mandate, then the Tenant's chain
head, runs the pure engine, and writes the Decision, its Audit record and the
idempotency entry together, or nothing at all (ADR-0003, ADR-0004).
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel

from api.deps import ClockDep, IdemKey, PackDep, StoreDep, TelemetryDep, Tenant
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from core.domain.audit import AuditRecord, DecisionInputs, seal
from core.domain.consent import CONSENT_TTL, ConsentRequest, ConsentRequestId, ConsentStatus
from core.domain.decisions import (
    AgentPaymentRequest,
    AuditRecordId,
    Decision,
    DecisionId,
    Outcome,
    PackRef,
    ReasonCode,
    RiskTier,
    month_window,
)
from core.domain.intents import IntentId
from core.domain.mandates import Mandate, MandateId
from core.domain.money import Money
from core.domain.parties import new_id
from core.domain.signals import IntentResolution, IntentSignal, Signals
from core.policy.engine import decide
from core.policy.pack import PolicyPack
from core.ports import Telemetry, TenantId, TenantTransaction

router = APIRouter(prefix="/v1/decisions", tags=["decisions"])


class StepUpOut(BaseModel):
    """What an Agent is told on step_up: wait, the Payer has been asked. Thresholds and
    caps are never included, so an Agent can't probe them by splitting payments."""

    consent_request_id: ConsentRequestId
    expires_at: AwareDatetime
    status_url: str


class ConsentOut(BaseModel):
    status: ConsentStatus


class DecisionOut(BaseModel):
    decision_id: DecisionId
    outcome: Outcome
    reason_codes: tuple[ReasonCode, ...]
    risk_tier: RiskTier
    policy_pack: PackRef
    audit_record_id: AuditRecordId
    decided_at: AwareDatetime
    step_up: StepUpOut | None
    consent: ConsentOut | None


def _out(decision: Decision, consent: ConsentRequest | None, now: datetime) -> DecisionOut:
    fields = decision.model_dump(include=set(DecisionOut.model_fields) - {"step_up", "consent"})
    if consent is None:
        return DecisionOut.model_validate({**fields, "step_up": None, "consent": None})
    step_up = StepUpOut(
        consent_request_id=consent.consent_request_id,
        expires_at=consent.expires_at,
        status_url=f"/v1/decisions/{decision.decision_id}",
    )
    return DecisionOut.model_validate(
        {**fields, "step_up": step_up, "consent": ConsentOut(status=consent.status(now))}
    )


def resolve_intent(
    tx: TenantTransaction, body: AgentPaymentRequest, mandate_id: MandateId, now: datetime
) -> tuple[IntentResolution, IntentSignal | None]:
    """Whether the cited Intent check may be used by this Decision: same Tenant (the
    transaction is scoped), Mandate, Agent, merchant and Declared MCC, and not expired."""
    if body.intent_id is None:
        return "none_cited", None
    check = tx.get_intent_check(IntentId(body.intent_id))
    if check is None:
        return "not_found", None
    if check.mandate_id != mandate_id:
        return "wrong_mandate", None
    if check.agent_id != body.agent_id:
        return "wrong_agent", None
    cart_merchant = check.content.merchant
    if (cart_merchant.id, cart_merchant.mcc) != (body.merchant.id, body.merchant.mcc):
        return "merchant_mismatch", None
    if now >= check.expires_at:
        return "expired", None
    return "used", IntentSignal(
        intent_id=check.intent_id,
        verdict=check.verdict,
        failure_cause=check.failure_cause,
        quoted_total=check.content.quoted_total,
        model=check.model,
        prompt_version=check.prompt_version,
        content_hash=check.content_hash,
    )


def compute_signals(
    tx: TenantTransaction,
    body: AgentPaymentRequest,
    mandate: Mandate,
    pack: PolicyPack,
    now: datetime,
    tenant: TenantId,
    telemetry: Telemetry,
) -> Signals:
    """Work out the Signals the pack uses, inside the Mandate lock, so parallel requests
    on one Mandate see consistent values. They are recorded and never recomputed."""
    mandate_id = mandate.mandate_id
    with telemetry.start_span("signals.compute", {"tenant_id": tenant}) as span:
        merchant_is_new = None
        if pack.merchant_new is not None:
            merchant_is_new = not tx.has_allowed_payment_to(mandate.payer_id, body.merchant.id)
            span.set_attribute("merchant_is_new", merchant_is_new)
        velocity_count = None
        if pack.velocity is not None:
            window = timedelta(seconds=pack.velocity.window_seconds)
            velocity_count = tx.count_decisions(mandate_id, now - window, now)
            span.set_attribute("velocity_count", velocity_count)
        resolution: IntentResolution | None = None
        intent = None
        if pack.intent is not None:
            resolution, intent = resolve_intent(tx, body, mandate_id, now)
            span.set_attribute("intent_resolution", resolution)
            if intent is not None:
                span.set_attribute("intent_verdict", intent.verdict)
        return Signals(
            velocity_count=velocity_count,
            merchant_is_new=merchant_is_new,
            cited_intent_id=None if body.intent_id is None else IntentId(body.intent_id),
            intent_resolution=resolution,
            intent=intent,
        )


def decision_time(now: datetime) -> datetime:
    """The Decision time: one server-read instant, truncated to milliseconds."""
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


@router.post("", response_model=DecisionOut)
def create_decision(
    request: Request,
    body: AgentPaymentRequest,
    tenant: Tenant,
    store: StoreDep,
    clock: ClockDep,
    pack: PackDep,
    telemetry: TelemetryDep,
    key: IdemKey,
) -> Response:
    def write(read_at: datetime) -> tuple[int, DecisionOut]:
        now = decision_time(read_at)
        mandate = tx.lock_mandate(body.mandate_id)
        if mandate is None:
            raise ApiError(404, "MANDATE_NOT_FOUND", f"No Mandate {body.mandate_id}.")
        # Summed inside the Mandate lock, so parallel requests can't both spend the same
        # headroom. Only allowed Decisions count; pending step_ups don't.
        spent = Money(
            minor=tx.allowed_spend(mandate.mandate_id, *month_window(now)),
            currency=mandate.currency,
        )
        agent = tx.get_agent(body.agent_id)
        signals = compute_signals(tx, body, mandate, pack.pack, now, tenant, telemetry)
        output = decide(body, mandate, agent, spent, signals, pack.pack, now)
        seq, prev_hash = tx.lock_chain_head()

        ref = PackRef(id=pack.pack.id, version=pack.pack.version, hash=pack.hash)
        decision = Decision(
            decision_id=DecisionId(new_id("dec")),
            tenant_id=tenant,
            mandate_id=mandate.mandate_id,
            agent_id=body.agent_id,
            merchant_id=body.merchant.id,
            amount=body.amount,
            outcome=output.outcome,
            reason_codes=output.reason_codes,
            risk_tier=output.risk_tier,
            policy_pack=ref,
            audit_record_id=AuditRecordId(new_id("aud")),
            decided_at=now,
        )
        sealed = seal(
            AuditRecord(
                audit_record_id=decision.audit_record_id,
                tenant_id=tenant,
                seq=seq + 1,
                prev_hash=prev_hash,
                kind="decision",
                decision_id=decision.decision_id,
                recorded_at=now,
                inputs=DecisionInputs(
                    request=body,
                    mandate=mandate,
                    agent=agent,
                    spent_this_month=spent,
                    signals=signals,
                    now=now,
                ),
                pack=ref,
                output=output,
            )
        )
        tx.add_decision(decision)
        consent = None
        if output.outcome == "step_up":
            consent = ConsentRequest(
                consent_request_id=ConsentRequestId(new_id("csr")),
                tenant_id=tenant,
                decision_id=decision.decision_id,
                created_at=now,
                expires_at=now + CONSENT_TTL,
            )
            tx.add_consent_request(consent)
        tx.append_audit_record(sealed)
        # Identifiers and codes only: no merchant name, line items or Agent reasoning.
        span.set_attribute("decision_id", decision.decision_id)
        span.set_attribute("outcome", output.outcome)
        if output.reason_codes:
            span.set_attribute("primary_reason_code", output.reason_codes[0])
        span.set_attribute("pack_id", ref.id)
        span.set_attribute("pack_version", ref.version)
        return 200, _out(decision, consent, now)

    # One span per request. A replayed idempotent response carries only the Tenant.
    with telemetry.start_span("decision", {"tenant_id": tenant}) as span:
        with store.transaction(tenant) as tx:
            return run_idempotent(tx, key, request_hash(request, body), clock, write)


@router.get("/{decision_id}")
def get_decision(decision_id: str, tenant: Tenant, store: StoreDep, clock: ClockDep) -> DecisionOut:
    """The Decision, with its Consent request's status if it was a step_up."""
    with store.transaction(tenant) as tx:
        decision = tx.get_decision(DecisionId(decision_id))
        consent = tx.get_consent_request(DecisionId(decision_id))
    if decision is None:
        raise ApiError(404, "DECISION_NOT_FOUND", f"No Decision {decision_id}.")
    return _out(decision, consent, clock.now())
