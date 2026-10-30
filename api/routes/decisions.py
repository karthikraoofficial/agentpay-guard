"""/v1/decisions: an Agent asks whether it may pay, and gets allow, step_up or block.

Every Outcome is HTTP 200. One transaction locks the Mandate, then the Tenant's chain
head, runs the pure engine, and writes the Decision, its Audit record and the
idempotency entry together, or nothing at all (ADR-0003, ADR-0004).
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel

from api.consent_view import mandate_inactive
from api.deps import (
    ClockDep,
    DecideTenant,
    IdemKey,
    ManageTenant,
    PackDep,
    SettingsDep,
    StoreDep,
    TelemetryDep,
    Tenant,
)
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from api.payability import payable_until, was_granted, window_end
from core.domain.audit import AuditRecord, DecisionInputs, seal
from core.domain.consent import ConsentRequest, ConsentRequestId, ConsentStatus
from core.domain.decisions import (
    AgentPaymentRequest,
    AuditRecordId,
    Decision,
    DecisionId,
    Outcome,
    PackRef,
    ReasonCode,
    RiskTier,
    decision_time,
    month_window,
)
from core.domain.intents import IntentId
from core.domain.mandates import Mandate, MandateId
from core.domain.money import Money
from core.domain.parties import new_id
from core.domain.signals import IntentResolution, IntentSignal, Signals
from core.domain.voids import VoidVia, void_refusal
from core.policy.engine import decide
from core.policy.pack import PolicyPack
from core.ports import DomainEvent, Telemetry, TenantId, TenantTransaction
from core.voids import record_void

router = APIRouter(prefix="/v1/decisions", tags=["decisions"])


class StepUpOut(BaseModel):
    """What an Agent is told on step_up: wait, the Payer has been asked. Thresholds and
    caps are never included, so an Agent can't probe them by splitting payments."""

    consent_request_id: ConsentRequestId
    expires_at: AwareDatetime
    status_url: str


class ConsentOut(BaseModel):
    status: ConsentStatus
    resolved_at: AwareDatetime | None
    # Only when an approval was blocked: why the Mandate no longer permits the payment.
    reason_codes: tuple[ReasonCode, ...] | None
    # Worked out when read: the Mandate is no longer active or the Agent was disabled,
    # so an approval now would be blocked.
    mandate_inactive: bool


class DecisionOut(BaseModel):
    decision_id: DecisionId
    outcome: Outcome
    reason_codes: tuple[ReasonCode, ...]
    risk_tier: RiskTier
    policy_pack: PackRef
    audit_record_id: AuditRecordId
    decided_at: AwareDatetime
    # The end of the Payment window: set on an allow, and on a step_up once approved.
    payable_until: AwareDatetime | None
    step_up: StepUpOut | None
    consent: ConsentOut | None


def _out(
    decision: Decision,
    consent: ConsentRequest | None,
    now: datetime,
    payable_until: datetime | None,
    inactive: bool = False,
) -> DecisionOut:
    fields = decision.model_dump(include=set(DecisionOut.model_fields)) | {
        "payable_until": payable_until
    }
    if consent is None:
        return DecisionOut.model_validate({**fields, "step_up": None, "consent": None})
    step_up = StepUpOut(
        consent_request_id=consent.consent_request_id,
        expires_at=consent.expires_at,
        status_url=f"/v1/decisions/{decision.decision_id}",
    )
    return DecisionOut.model_validate(
        {**fields, "step_up": step_up, "consent": _consent_out(consent, now, inactive)}
    )


def _consent_out(consent: ConsentRequest, now: datetime, inactive: bool) -> ConsentOut:
    resolution = consent.resolution
    return ConsentOut(
        status=consent.status(now),
        resolved_at=resolution.resolved_at if resolution else None,
        reason_codes=resolution.reason_codes if resolution else None,
        mandate_inactive=inactive,
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


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


@router.post("", response_model=DecisionOut)
def create_decision(
    request: Request,
    body: AgentPaymentRequest,
    tenant: DecideTenant,
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
                expires_at=now + pack.pack.consent_ttl,
            )
            tx.add_consent_request(consent)
            # The workflow is started from the outbox, never from this hot path (ADR-0009).
            tx.publish(
                DomainEvent(
                    event_id=new_id("evt"),
                    tenant_id=tenant,
                    event_type="consent_requested",
                    occurred_at=now,
                    payload={
                        "consent_request_id": consent.consent_request_id,
                        "expires_at": _iso(consent.expires_at),
                    },
                )
            )
        tx.append_audit_record(sealed)
        # Identifiers and codes only: no merchant name, line items or Agent reasoning.
        span.set_attribute("decision_id", decision.decision_id)
        span.set_attribute("outcome", output.outcome)
        if output.reason_codes:
            span.set_attribute("primary_reason_code", output.reason_codes[0])
        span.set_attribute("pack_id", ref.id)
        span.set_attribute("pack_version", ref.version)
        until = window_end(now, pack.pack) if output.outcome == "allow" else None
        return 200, _out(decision, consent, now, until)

    # One span per request. A replayed idempotent response carries only the Tenant.
    with telemetry.start_span("decision", {"tenant_id": tenant}) as span:
        with store.transaction(tenant) as tx:
            return run_idempotent(tx, key, request_hash(request, body), clock, write)


@router.get("/{decision_id}")
def get_decision(
    decision_id: str,
    tenant: Tenant,
    store: StoreDep,
    clock: ClockDep,
    pack: PackDep,
    settings: SettingsDep,
) -> DecisionOut:
    """The Decision, with its Consent request's status if it was a step_up, and until when
    it can be paid."""
    now = clock.now()
    with store.transaction(tenant) as tx:
        decision = tx.get_decision(DecisionId(decision_id))
        consent = tx.get_consent_request(DecisionId(decision_id))
        if decision is None:
            raise ApiError(404, "DECISION_NOT_FOUND", f"No Decision {decision_id}.")
        inactive = consent is not None and mandate_inactive(
            tx.get_mandate(decision.mandate_id), tx.get_agent(decision.agent_id), now
        )
        until = payable_until(tx, decision, consent, pack, settings.policy_packs_dir)
    return _out(decision, consent, now, until, inactive)


class VoidOut(BaseModel):
    decision_id: DecisionId
    via: VoidVia
    voided_at: AwareDatetime
    audit_record_id: AuditRecordId


_VOID_REFUSAL_TEXT = {
    "DECISION_ALREADY_VOIDED": "This Decision is already Voided.",
    "DECISION_HAS_PAYMENT": "This Decision has a Payment, so it can't be Voided.",
    "DECISION_NOT_PAYABLE": "Only an allow, or an approved step-up, can be Voided.",
}


@router.post("/{decision_id}/void", response_model=VoidOut)
def void_decision(
    request: Request,
    decision_id: str,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    key: IdemKey,
) -> Response:
    """Release an unpaid allow, or approved step-up, from Spent this month, even after its
    Payment window closed. The Decision itself never changes, and it can't be paid after."""

    def write(read_at: datetime) -> tuple[int, VoidOut]:
        now = decision_time(read_at)
        decision = tx.get_decision(DecisionId(decision_id))
        if decision is None:
            raise ApiError(404, "DECISION_NOT_FOUND", f"No Decision {decision_id}.")
        # Mandate first, as Decisions do, so none counts this spend while the Void commits.
        if tx.lock_mandate(decision.mandate_id) is None:
            raise RuntimeError(f"Mandate {decision.mandate_id} vanished")
        consent = tx.get_consent_request(decision.decision_id)
        refusal = void_refusal(
            already_voided=tx.get_void(decision.decision_id) is not None,
            has_payment=tx.get_payment_for_decision(decision.decision_id) is not None,
            was_payable=was_granted(decision, consent),
        )
        if refusal is not None:
            raise ApiError(409, refusal, _VOID_REFUSAL_TEXT[refusal])
        void = record_void(tx, decision.decision_id, "tenant_api", now)
        return 200, VoidOut.model_validate(void.model_dump(include=set(VoidOut.model_fields)))

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, None), clock, write)
