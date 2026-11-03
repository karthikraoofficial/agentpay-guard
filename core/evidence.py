"""Building a Dispute's Evidence pack (ADR-0018).

Code gathers the Evidence bundle by fixed rules, through four readers named as the tools
a model would use (`get_audit_trail`, `get_mandate`, `get_consent_record`,
`get_rail_events`), so a tool-using agent could later sit behind them without changing
the pack. The bundle reads the Audit chain only up to the Dispute's `dispute_opened`
record, so it is the same however much the chain grows.

The dispute evidence agent (agents.dispute_narrative) writes the narrative from the
bundle when it can; the citation checker refuses anything it can't trace to the bundle.
Otherwise the narrative here is written by code: one sentence per fact, each citing the
Audit records it rests on.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.domain.audit import AuditKind, AuditRecord, SealedAuditRecord
from core.domain.decisions import DecisionId, PackRef, decision_time
from core.domain.disputes import CLOSED, Dispute, DisputeId
from core.domain.evidence import (
    BundleRecord,
    Claim,
    ClaimKind,
    ConsentRecord,
    EvidenceBundle,
    EvidencePack,
    NarrativeAttempt,
    RailEventRecord,
)
from core.domain.mandates import Mandate
from core.domain.money import Money
from core.domain.payments import PaymentId
from core.evidence_strength import assess
from core.policy.packs import load_active_pack
from core.ports import Clock, TenantId, TenantTransaction, UnitOfWork
from core.tenant_events import dispute_changed

# --- The readers ---------------------------------------------------------------------


def get_audit_trail(
    tx: TenantTransaction, decision_ids: set[DecisionId], up_to_seq: int
) -> tuple[BundleRecord, ...]:
    """These Decisions' Audit records, in chain order, up to `up_to_seq`."""
    return tuple(_bundled(s) for s in tx.audit_records_for(decision_ids, up_to_seq))


def get_mandate(trail: tuple[BundleRecord, ...]) -> Mandate:
    """The Mandate as the Decision saw it: the snapshot in its Audit record, never the
    Mandate as it is now."""
    inputs = trail[0].record.inputs
    if trail[0].kind != "decision" or inputs is None:
        raise ValueError("a Decision's trail starts with its decision record")
    return inputs.mandate


def get_consent_record(tx: TenantTransaction, decision_id: DecisionId) -> ConsentRecord | None:
    consent = tx.get_consent_request(decision_id)
    if consent is None:
        return None
    return ConsentRecord(
        consent_request_id=consent.consent_request_id,
        created_at=consent.created_at,
        expires_at=consent.expires_at,
    )


def get_rail_events(
    tx: TenantTransaction, payment_id: PaymentId, until: datetime
) -> tuple[RailEventRecord, ...]:
    """The rail events kept for the Payment, received by `until`."""
    return tuple(
        RailEventRecord(
            event_id=e.event_id,
            event_type=e.event_type,
            rail_payment_id=e.rail_payment_id,
            status=e.status,
            failure_code=e.failure_code,
            received_at=e.received_at,
        )
        for e in tx.list_rail_events(payment_id)
        if e.received_at <= until
    )


def _bundled(sealed: SealedAuditRecord) -> BundleRecord:
    return BundleRecord(
        audit_record_id=sealed.record.audit_record_id,
        seq=sealed.seq,
        kind=sealed.record.kind,
        record_hash=sealed.record_hash,
        record=sealed.record,
    )


# --- The bundle ------------------------------------------------------------------------


def gather_bundle(tx: TenantTransaction, dispute: Dispute) -> EvidenceBundle:
    trail = get_audit_trail(tx, {dispute.decision_id}, dispute.opened_seq)
    decision = tx.get_decision(dispute.decision_id)
    if decision is None:
        raise RuntimeError(f"Decision {dispute.decision_id} vanished")
    others = {
        d.decision_id
        for d in tx.list_related_decisions(
            decision.mandate_id, decision.agent_id, decision.merchant_id
        )
        if d.decision_id != decision.decision_id
    }
    related = tuple(
        r for r in get_audit_trail(tx, others, dispute.opened_seq) if r.kind == "decision"
    )
    mandate = get_mandate(trail)
    elsewhere = {
        d.decision_id
        for d in tx.list_payer_decisions(mandate.payer_id, decision.merchant_id)
        if d.decision_id != decision.decision_id
    }
    payer_payments = tuple(
        r
        for r in get_audit_trail(tx, elsewhere, dispute.opened_seq)
        if r.kind == "payment" and r.record.payment and r.record.payment.status == "captured"
    )
    live = tx.get_mandate(mandate.mandate_id)
    revoked_at = live.revoked_at if live else None
    return EvidenceBundle(
        dispute_id=dispute.dispute_id,
        trail=trail,
        mandate=mandate,
        consent=get_consent_record(tx, dispute.decision_id),
        rail_events=get_rail_events(tx, dispute.payment_id, dispute.created_at),
        related=related,
        mandate_revoked_at=revoked_at if revoked_at and revoked_at <= dispute.created_at else None,
        payer_payments=payer_payments,
    )


# --- Building --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelNarrative:
    """What a narrative writer gives back: claims the checker accepted, or None, and
    every model call it made."""

    claims: tuple[Claim, ...] | None
    attempts: tuple[NarrativeAttempt, ...]


# Writes a model narrative for a bundle (agents.dispute_narrative). Given a bundle that
# code gathered, it never chooses what to read (ADR-0018).
NarrativeWriter = Callable[[TenantId, EvidenceBundle], ModelNarrative]

UNAVAILABLE = "model narrative unavailable"


def build_evidence_pack(
    store: UnitOfWork,
    tenant_id: TenantId,
    dispute_id: DisputeId,
    clock: Clock,
    packs_dir: Path,
    writer: NarrativeWriter | None = None,
) -> str:
    """Build the next pack version of an open Dispute and put it in review, assessed with
    the Policy pack active now. Driven by the Dispute's status, so it is safe to run more
    than once: returns `built`, `already_built` (in review), `closed` (signed off, expired
    or withdrawn, so a build in progress writes nothing) or `missing`.

    The bundle is gathered in one transaction and the pack written in another, so no
    database connection is held while the model writes. The second transaction writes
    only if the Dispute is still open with no newer pack."""
    with store.transaction(tenant_id) as tx:
        dispute = tx.lock_dispute(dispute_id)
        if dispute is None:
            return "missing"
        if dispute.status in CLOSED:
            return "closed"
        if dispute.status == "in_review":
            return "already_built"
        latest = tx.get_evidence_pack(dispute_id)
        version = 1 if latest is None else latest.version + 1
        bundle = gather_bundle(tx, dispute)

    written = None if writer is None else writer(tenant_id, bundle)
    if written is not None and written.claims is not None:
        narrative, source, note = written.claims, "model", None
    else:
        narrative, source = template_narrative(bundle), "template"
        note = UNAVAILABLE if written is not None else None
    policy = load_active_pack(packs_dir, "default")
    rules = policy.pack.disputes
    now = decision_time(clock.now())

    with store.transaction(tenant_id) as tx:
        dispute = tx.lock_dispute(dispute_id)
        if dispute is None:
            return "missing"
        if dispute.status in CLOSED:
            return "closed"
        current = tx.get_evidence_pack(dispute_id)
        if dispute.status == "in_review" or (current and current.version >= version):
            return "already_built"
        tx.add_evidence_pack(
            EvidencePack(
                dispute_id=dispute_id,
                version=version,
                bundle=bundle,
                bundle_hash=bundle.hash(),
                narrative=narrative,
                narrative_source=source,
                built_at=now,
                policy_pack=PackRef(
                    id=policy.pack.id, version=policy.pack.version, hash=policy.hash
                ),
                assessment=None if rules is None else assess(bundle, dispute.reason, rules),
                narrative_attempts=() if written is None else written.attempts,
                narrative_note=note,
            )
        )
        in_review = dispute.model_copy(update={"status": "in_review", "updated_at": now})
        tx.update_dispute(in_review)
        tx.publish(dispute_changed(in_review, version, now))
    return "built"


# --- The template narrative ------------------------------------------------------------


def template_narrative(bundle: EvidenceBundle) -> tuple[Claim, ...]:
    """One sentence per fact in the bundle, each citing the record it comes from."""
    claims = [_mandate_claim(bundle.mandate, bundle.trail[0])]
    for item in bundle.trail:
        claims.append(_claim(item))
    for item in bundle.related:
        claims.append(_related_claim(item))
    for item in bundle.payer_payments:
        claims.append(_payer_payment_claim(item))
    return tuple(claims)


def _mandate_claim(mandate: Mandate, decision: BundleRecord) -> Claim:
    evidence = mandate.consent_evidence
    return Claim(
        text=(
            f"Mandate {mandate.mandate_id} let Agents {', '.join(mandate.allowed_agent_ids)} "
            f"spend up to {_money(mandate.per_txn_cap)} a payment and "
            f"{_money(mandate.monthly_cap)} a month at MCCs {', '.join(mandate.allowed_mccs)}, "
            f"from {_at(mandate.valid_from)} to {_at(mandate.expires_at)}. The Tenant "
            f"attests the Payer agreed by {evidence.method} (reference {evidence.reference}) "
            f"on {_at(evidence.captured_at)}."
        ),
        cites=(decision.audit_record_id,),
        kind="mandate",
    )


def _claim(item: BundleRecord) -> Claim:
    record = item.record
    return Claim(text=_sentence(record), cites=(item.audit_record_id,), kind=_KIND[item.kind])


def _sentence(record: AuditRecord) -> str:
    at = _at(record.recorded_at)
    if record.kind == "decision" and record.inputs and record.output and record.pack:
        request = record.inputs.request
        reasons = record.output.reason_codes
        return (
            f"On {at}, Agent {request.agent_id} asked to pay {request.merchant.name} "
            f"(MCC {request.merchant.mcc}) {_money(request.amount)}, and AgentPay Guard "
            f"decided {record.output.outcome} with Policy pack "
            f"{record.pack.id}@{record.pack.version}"
            + (f" (reason codes {', '.join(reasons)})." if reasons else ".")
        )
    if record.kind == "consent_resolution" and record.resolution:
        resolution = record.resolution
        attested = resolution.attestation
        return f"On {at}, the step-up was {resolution.status} via {resolution.resolved_via}" + (
            f"; the Tenant attests the Payer confirmed by {attested.method} "
            f"(reference {attested.reference})."
            if attested
            else "."
        )
    if record.kind == "payment" and record.payment:
        payment = record.payment
        rail = f" on {payment.rail} as {payment.rail_payment_id}" if payment.rail_payment_id else ""
        failed = f" with code {payment.failure_code}" if payment.failure_code else ""
        return (
            f"On {at}, Payment {payment.payment_id} for {_money(payment.amount)} was "
            f"{payment.status}{rail}{failed}."
        )
    if record.kind == "void" and record.void:
        return f"On {at}, the Decision was Voided ({record.void.via})."
    if record.kind == "dispute_opened" and record.dispute:
        dispute = record.dispute
        return (
            f"On {at}, the Tenant reported a Dispute of Payment {dispute.payment_id} "
            f"as {dispute.reason}, to be answered by {_at(dispute.respond_by)}."
        )
    raise ValueError(f"no sentence for a {record.kind} record")


_KIND: dict[AuditKind, ClaimKind] = {
    "decision": "decision",
    "consent_resolution": "payer_approval",
    "payment": "payment",
    "void": "void",
    "dispute_opened": "dispute",
    "dispute_signed_off": "dispute",
}


def _related_claim(item: BundleRecord) -> Claim:
    record = item.record
    if record.inputs is None or record.output is None:
        raise ValueError("a related record is a decision record")
    request = record.inputs.request
    reasons = record.output.reason_codes
    return Claim(
        text=(
            f"Also on {_at(record.recorded_at)}, the same Agent asked to pay "
            f"{request.merchant.name} {_money(request.amount)} under this Mandate, and "
            f"AgentPay Guard decided {record.output.outcome}"
            + (f" (reason codes {', '.join(reasons)})." if reasons else ".")
        ),
        cites=(item.audit_record_id,),
        kind="context",
    )


def _payer_payment_claim(item: BundleRecord) -> Claim:
    payment = item.record.payment
    if payment is None:
        raise ValueError("a Payer payment record is a payment record")
    return Claim(
        text=(
            f"On {_at(item.record.recorded_at)}, another Payment by the same Payer to this "
            f"merchant, {payment.payment_id} for {_money(payment.amount)}, was captured."
        ),
        cites=(item.audit_record_id,),
        kind="context",
    )


# ISO 4217 currencies with no minor unit; every other currency here has two.
_ZERO_DECIMAL = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF"})


def _money(amount: Money) -> str:
    if amount.currency in _ZERO_DECIMAL:
        return f"{amount.currency} {amount.minor:,}"
    whole, cents = divmod(amount.minor, 100)
    return f"{amount.currency} {whole:,}.{cents:02d}"


def _at(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M UTC")
