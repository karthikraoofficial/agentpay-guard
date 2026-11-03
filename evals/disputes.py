"""The Dispute golden sets: network codes and Evidence strength for labelled scenarios,
and the citation checker's verdict on labelled model replies.

Each case in `evals/golden/disputes*.jsonl` describes a disputed Payment in a few fields
(how it was decided, any Intent check and approval, the amounts, a Revocation, other
Payments by the Payer to the merchant). `bundle_for` turns that into an Evidence bundle of
real Audit records, and `assess` is scored against the label. A case lists only what it
changes from `<name>_base.json`, whose `pack` names the Policy pack it is labelled for.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.domain.audit import AuditRecord, DecisionInputs, PaymentEvidence
from core.domain.consent import Attestation, ConsentRequestId, ResolutionEvidence
from core.domain.decisions import (
    AgentPaymentRequest,
    AuditRecordId,
    DecisionId,
    EngineOutput,
    Merchant,
    Outcome,
    PackRef,
)
from core.domain.disputes import DisputeId, DisputeReason, EvidenceFact, EvidenceStrength
from core.domain.evidence import BundleRecord, CheckCode, EvidenceBundle
from core.domain.intents import IntentId, IntentVerdict
from core.domain.mandates import Mandate
from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
from core.domain.signals import IntentSignal, Signals
from core.evidence_strength import assess
from core.narrative_check import check_narrative
from core.policy.packs import load_pack

_ZERO = "sha256:" + "00" * 32


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Pin(_Strict):
    id: str
    version: str


class Intent(_Strict):
    verdict: IntentVerdict
    quoted_total_minor: int


class Approval(_Strict):
    attested: bool


class OtherPayment(_Strict):
    """Another captured Payment by the Payer to the merchant, relative to this capture."""

    amount_minor: int
    hours_from_capture: float


class Expected(_Strict):
    strength: EvidenceStrength
    visa_code: str
    mastercard_code: str
    missing: tuple[EvidenceFact, ...]


class Scenario(_Strict):
    """A disputed Payment, described in a few fields: what `bundle_for` builds from."""

    pack: Pin
    mandate: dict[str, Any]
    outcome: Outcome
    decision_amount_minor: int
    payment_amount_minor: int
    intent: Intent | None = None
    approval: Approval | None = None
    mandate_revoked: Literal["before_decision", "before_capture", "after_capture"] | None = None
    other_payments: tuple[OtherPayment, ...] = ()
    decided_at: datetime
    captured_at: datetime
    # Text from the Agent's side, which a live eval fills with prompt injections.
    merchant_name: str = "Eval Mart"
    agent_reasoning: str | None = None


class DisputeCase(Scenario):
    id: str
    description: str
    reason: DisputeReason
    expected: Expected


class Miss(_Strict):
    case_id: str
    description: str
    expected: Expected
    actual: Expected


class DisputeReport(_Strict):
    total: int
    mismatches: list[Miss] = Field(default_factory=list)


def load_dispute_cases(path: Path) -> list[DisputeCase]:
    base = json.loads((path.parent / f"{path.stem}_base.json").read_text(encoding="utf-8"))
    base.pop("_about", None)
    lines = path.read_text(encoding="utf-8").splitlines()
    return [DisputeCase.model_validate(base | json.loads(line)) for line in lines if line.strip()]


def run_dispute_cases(cases: list[DisputeCase], pack_root: Path) -> DisputeReport:
    misses = []
    for case in cases:
        rules = load_pack(pack_root, case.pack.id, case.pack.version).pack.disputes
        if rules is None:
            raise ValueError(f"{case.pack.id}@{case.pack.version} has no Dispute rules")
        got = assess(bundle_for(case), case.reason, rules)
        actual = Expected(
            strength=got.strength,
            visa_code=got.visa_code,
            mastercard_code=got.mastercard_code,
            missing=got.missing,
        )
        if actual != case.expected:
            misses.append(
                Miss(
                    case_id=case.id,
                    description=case.description,
                    expected=case.expected,
                    actual=actual,
                )
            )
    return DisputeReport(total=len(cases), mismatches=misses)


# --- Building a bundle from a case ------------------------------------------------------


def bundle_for(case: Scenario) -> EvidenceBundle:
    revoked = {
        "before_decision": case.decided_at - timedelta(hours=1),
        "before_capture": case.decided_at + (case.captured_at - case.decided_at) / 2,
        "after_capture": case.captured_at + timedelta(hours=1),
        None: None,
    }[case.mandate_revoked]
    # The snapshot is the Mandate as the Decision saw it: revoked only if that was earlier.
    earlier = {"revoked_at": revoked, "revoked_by": "payer"}
    snapshot = Mandate.model_validate(
        case.mandate | (earlier if case.mandate_revoked == "before_decision" else {})
    )
    currency = snapshot.currency
    signals = Signals()
    if case.intent is not None:
        signals = Signals(
            cited_intent_id=IntentId("int_eval"),
            intent_resolution="used",
            intent=IntentSignal(
                intent_id=IntentId("int_eval"),
                verdict=case.intent.verdict,
                failure_cause=None,
                quoted_total=Money(minor=case.intent.quoted_total_minor, currency=currency),
                model="eval",
                prompt_version="eval",
                content_hash=_ZERO,
            ),
        )
    request = AgentPaymentRequest(
        mandate_id=snapshot.mandate_id,
        agent_id=snapshot.allowed_agent_ids[0],
        amount=Money(minor=case.decision_amount_minor, currency=currency),
        merchant=Merchant(id="m_eval", name=case.merchant_name, mcc=snapshot.allowed_mccs[0]),
        agent_reasoning=case.agent_reasoning,
    )
    inputs = DecisionInputs(
        request=request,
        mandate=snapshot,
        agent=None,
        spent_this_month=Money(minor=0, currency=currency),
        signals=signals,
        now=case.decided_at,
    )
    pack = PackRef(id=case.pack.id, version=case.pack.version, hash=_ZERO)
    trail = [
        _record(
            1, "decision", case.decided_at, inputs=inputs, pack=pack, output=_output(case.outcome)
        )
    ]
    if case.approval is not None:
        attestation = (
            Attestation(method="in_app_confirm", reference="eval", captured_at=case.decided_at)
            if case.approval.attested
            else None
        )
        trail.append(
            _record(
                2,
                "consent_resolution",
                case.decided_at + timedelta(minutes=2),
                # An approval re-runs the engine (ADR-0010); here it allowed.
                inputs=inputs,
                pack=pack,
                output=_output("allow"),
                resolution=ResolutionEvidence(
                    consent_request_id=ConsentRequestId("csr_eval"),
                    status="approved",
                    resolved_via="tenant_api",
                    attestation=attestation,
                ),
            )
        )
    paid = Money(minor=case.payment_amount_minor, currency=currency)
    trail.append(_payment(len(trail) + 1, case.captured_at, "pmt_eval", paid))
    payer_payments = tuple(
        _payment(
            100 + i,
            case.captured_at + timedelta(hours=other.hours_from_capture),
            f"pmt_other_{i}",
            Money(minor=other.amount_minor, currency=currency),
        )
        for i, other in enumerate(case.other_payments)
    )
    return EvidenceBundle(
        dispute_id=DisputeId("dsp_eval"),
        trail=tuple(trail),
        mandate=snapshot,
        consent=None,
        rail_events=(),
        related=(),
        mandate_revoked_at=revoked,
        payer_payments=payer_payments,
    )


def _output(outcome: Outcome) -> EngineOutput:
    return EngineOutput(outcome=outcome, reason_codes=(), risk_tier="low", rule_results=())


def _payment(seq: int, at: datetime, payment_id: str, amount: Money) -> BundleRecord:
    return _record(
        seq,
        "payment",
        at,
        payment=PaymentEvidence(
            payment_id=PaymentId(payment_id),
            status="captured",
            amount=amount,
            rail="mock",
            rail_payment_id=f"rail_{payment_id}",
            failure_code=None,
        ),
    )


def _record(seq: int, kind: Any, at: datetime, **sections: Any) -> BundleRecord:
    record_id = AuditRecordId(f"aud_eval_{seq}")
    record = AuditRecord(
        audit_record_id=record_id,
        tenant_id=TenantId("ten_eval"),
        seq=seq,
        prev_hash=_ZERO,
        kind=kind,
        decision_id=DecisionId(f"dec_other_{seq}" if seq >= 100 else "dec_eval"),
        recorded_at=at,
        **sections,
    )
    return BundleRecord(
        audit_record_id=record_id, seq=seq, kind=kind, record_hash=_ZERO, record=record
    )


# --- The citation checker ----------------------------------------------------------------


class NarrativeExpected(_Strict):
    accepted: bool
    codes: tuple[CheckCode, ...]


class NarrativeCase(Scenario):
    """A model reply and what the checker must make of it against the scenario's bundle."""

    id: str
    description: str
    reply: str
    stop_reason: str = "end_turn"
    expected: NarrativeExpected


class NarrativeMiss(_Strict):
    case_id: str
    description: str
    expected: NarrativeExpected
    actual: NarrativeExpected


class NarrativeReport(_Strict):
    total: int
    mismatches: list[NarrativeMiss] = Field(default_factory=list)


def load_narrative_cases(path: Path) -> list[NarrativeCase]:
    base = json.loads((path.parent / f"{path.stem}_base.json").read_text(encoding="utf-8"))
    base.pop("_about", None)
    lines = path.read_text(encoding="utf-8").splitlines()
    return [NarrativeCase.model_validate(base | json.loads(line)) for line in lines if line.strip()]


def run_narrative_cases(cases: list[NarrativeCase]) -> NarrativeReport:
    misses = []
    for case in cases:
        claims, failures = check_narrative(case.reply, case.stop_reason, bundle_for(case))
        actual = NarrativeExpected(
            accepted=not failures and bool(claims), codes=tuple(f.code for f in failures)
        )
        if actual != case.expected:
            misses.append(
                NarrativeMiss(
                    case_id=case.id,
                    description=case.description,
                    expected=case.expected,
                    actual=actual,
                )
            )
    return NarrativeReport(total=len(cases), mismatches=misses)
