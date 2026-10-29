"""/v1/decisions: an Agent asks whether it may pay, and gets allow, step_up or block.

Every Outcome is HTTP 200. One transaction locks the Mandate, then the Tenant's chain
head, runs the pure engine, and writes the Decision, its Audit record and the
idempotency entry together, or nothing at all (ADR-0003, ADR-0004).
"""

from datetime import datetime

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel

from api.deps import ClockDep, IdemKey, PackDep, StoreDep, Tenant
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from core.domain.audit import AuditRecord, DecisionInputs, seal
from core.domain.decisions import (
    AgentPaymentRequest,
    AuditRecordId,
    Decision,
    DecisionId,
    Outcome,
    PackRef,
    ReasonCode,
    RiskTier,
)
from core.domain.money import Money
from core.domain.parties import new_id
from core.policy.engine import decide

router = APIRouter(prefix="/v1/decisions", tags=["decisions"])


class DecisionOut(BaseModel):
    decision_id: DecisionId
    outcome: Outcome
    reason_codes: tuple[ReasonCode, ...]
    risk_tier: RiskTier
    policy_pack: PackRef
    audit_record_id: AuditRecordId
    decided_at: AwareDatetime
    step_up: None = None


def _out(decision: Decision) -> DecisionOut:
    return DecisionOut.model_validate(
        decision.model_dump(include=set(DecisionOut.model_fields) - {"step_up"})
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
    key: IdemKey,
) -> Response:
    def write(read_at: datetime) -> tuple[int, DecisionOut]:
        now = decision_time(read_at)
        mandate = tx.lock_mandate(body.mandate_id)
        if mandate is None:
            raise ApiError(404, "MANDATE_NOT_FOUND", f"No Mandate {body.mandate_id}.")
        agent = tx.get_agent(body.agent_id)
        seq, prev_hash = tx.lock_chain_head()
        spent = Money(minor=0, currency=mandate.currency)  # summed from allowed Decisions in #21
        output = decide(body, mandate, agent, spent, pack.pack, now)

        ref = PackRef(id=pack.pack.id, version=pack.pack.version, hash=pack.hash)
        decision = Decision(
            decision_id=DecisionId(new_id("dec")),
            tenant_id=tenant,
            mandate_id=mandate.mandate_id,
            agent_id=body.agent_id,
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
                    request=body, mandate=mandate, agent=agent, spent_this_month=spent, now=now
                ),
                pack=ref,
                output=output,
            )
        )
        tx.add_decision(decision)
        tx.append_audit_record(sealed)
        return 200, _out(decision)

    with store.transaction(tenant) as tx:
        return run_idempotent(tx, key, request_hash(request, body), clock, write)


@router.get("/{decision_id}")
def get_decision(decision_id: str, tenant: Tenant, store: StoreDep) -> DecisionOut:
    with store.transaction(tenant) as tx:
        decision = tx.get_decision(DecisionId(decision_id))
    if decision is None:
        raise ApiError(404, "DECISION_NOT_FOUND", f"No Decision {decision_id}.")
    return _out(decision)
