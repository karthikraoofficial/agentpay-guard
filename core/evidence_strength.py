"""Evidence strength: what the Policy pack's Dispute rules make of an Evidence bundle.

Fixed code works out which Evidence facts hold for the bundle; the pack says, per Dispute
reason, which facts make the evidence strong and which make it weak (ADR-0002). No model
is involved, and the same bundle and pack always give the same assessment.
"""

from datetime import datetime, timedelta

from core.domain.audit import AuditRecord
from core.domain.disputes import DisputeReason, EvidenceFact, EvidenceStrength
from core.domain.evidence import Assessment, EvidenceBundle
from core.policy.pack import DisputeRules

_NOTES: dict[EvidenceFact, str] = {
    "fulfilment_evidence": (
        "AgentPay Guard holds no evidence of what was delivered; add the merchant's own "
        "fulfilment evidence."
    ),
    "payer_intent_confirmed": (
        "No Intent match and no attested approval show the Payer wanted this purchase."
    ),
    "within_quoted_total": "No Quoted total shows the amount was what the Agent was quoted.",
}


def assess(bundle: EvidenceBundle, reason: DisputeReason, rules: DisputeRules) -> Assessment:
    held = evidence_facts(bundle, timedelta(seconds=rules.duplicate_window_seconds))
    weighed = rules.reasons[reason]
    missing = tuple(f for f in weighed.strong_when if f not in held)
    strength: EvidenceStrength
    if any(f in held for f in weighed.weak_when):
        strength = "weak"
    elif not missing:
        strength = "strong"
    else:
        strength = "mixed"
    return Assessment(
        reason=reason,
        visa_code=weighed.network_codes.visa,
        mastercard_code=weighed.network_codes.mastercard,
        strength=strength,
        facts=tuple(sorted(held)),
        missing=missing,
        notes=tuple(_NOTES[f] for f in missing if f in _NOTES),
    )


def evidence_facts(bundle: EvidenceBundle, duplicate_window: timedelta) -> frozenset[EvidenceFact]:
    """The Evidence facts that hold for the bundle. `fulfilment_evidence` never does."""
    decision = bundle.trail[0].record
    inputs, output = decision.inputs, decision.output
    if inputs is None or output is None:
        raise ValueError("a Decision's trail starts with its decision record")
    approval = next(
        (r.record.resolution for r in bundle.trail if r.kind == "consent_resolution"), None
    )
    approved = approval is not None and approval.status == "approved"
    attested = approval is not None and approved and approval.attestation is not None
    captured = next(
        (
            r.record
            for r in bundle.trail
            if r.kind == "payment" and r.record.payment and r.record.payment.status == "captured"
        ),
        None,
    )
    intent = inputs.signals.intent if inputs.signals.intent_resolution == "used" else None
    decided_at = inputs.now
    revoked_at = bundle.mandate.revoked_at or bundle.mandate_revoked_at

    facts: set[EvidenceFact] = set()
    if output.outcome == "allow" or (output.outcome == "step_up" and approved):
        facts.add("decision_within_mandate")
    if (output.outcome == "allow" and intent and intent.verdict == "match") or attested:
        facts.add("payer_intent_confirmed")
    if approved and intent and intent.verdict == "mismatch":
        facts.add("intent_mismatch_approved")
    if revoked_at is not None and revoked_at <= decided_at:
        facts.add("mandate_revoked_before_decision")
    elif bundle.mandate.status(decided_at) == "active":
        facts.add("mandate_active_at_decision")
    if (
        revoked_at is not None
        and captured is not None
        and decided_at < revoked_at < captured.recorded_at
    ):
        facts.add("mandate_revoked_before_capture")
    if captured is not None and captured.payment is not None:
        amount = captured.payment.amount
        if amount == inputs.request.amount:
            facts.add("amount_matches_decision")
        else:
            facts.add("amount_differs_from_decision")
        if (
            intent is not None
            and amount.currency == intent.quoted_total.currency
            and (amount.minor <= intent.quoted_total.minor)
        ):
            facts.add("within_quoted_total")
        if _duplicate(bundle, captured, duplicate_window):
            facts.add("duplicate_payment_found")
        else:
            facts.add("no_duplicate_payment")
    return frozenset(facts)


def _duplicate(bundle: EvidenceBundle, captured: AuditRecord, window: timedelta) -> bool:
    if captured.payment is None:
        return False
    amount, at = captured.payment.amount, captured.recorded_at
    return any(
        other.record.payment is not None
        and other.record.payment.amount == amount
        and _within(other.record.recorded_at, at, window)
        for other in bundle.payer_payments
    )


def _within(a: datetime, b: datetime, window: timedelta) -> bool:
    return abs(a - b) <= window
