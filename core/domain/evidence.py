"""The Evidence bundle and Evidence pack for a Dispute (see GLOSSARY.md, ADR-0018).

The bundle is gathered by fixed rules and hashed, so the same Dispute always gives the
same bundle. A pack is one version of what is built from it: the bundle and a narrative
whose every claim cites Audit records in the bundle."""

import hashlib
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from core.domain.audit import AuditKind, AuditRecord
from core.domain.canonical_json import canonical_json
from core.domain.consent import ConsentRequestId
from core.domain.decisions import AuditRecordId, PackRef
from core.domain.disputes import DisputeId, DisputeReason, EvidenceFact, EvidenceStrength
from core.domain.mandates import Mandate


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BundleRecord(_Frozen):
    """One Audit record as the bundle carries it: where it sits in the chain and what it
    says. A claim cites it by `audit_record_id`."""

    audit_record_id: AuditRecordId
    seq: int
    kind: AuditKind
    record_hash: str
    record: AuditRecord


class ConsentRecord(_Frozen):
    """The Consent request a step_up raised. How it ended is its `consent_resolution`
    Audit record."""

    consent_request_id: ConsentRequestId
    created_at: AwareDatetime
    expires_at: AwareDatetime


class RailEventRecord(_Frozen):
    """A rail event we acted on: ids, status and code only, never the payload."""

    event_id: str
    event_type: str
    rail_payment_id: str
    status: str
    failure_code: str | None
    received_at: AwareDatetime


class EvidenceBundle(_Frozen):
    dispute_id: DisputeId
    # The disputed Decision's records, in chain order, up to its `dispute_opened` record.
    trail: tuple[BundleRecord, ...]
    # The Mandate as the Decision saw it: the snapshot in its Audit record.
    mandate: Mandate
    consent: ConsentRecord | None
    rail_events: tuple[RailEventRecord, ...]
    # Other Decisions by the same Agent at the same merchant under the Mandate, made
    # before the Dispute was opened: context, such as an earlier attempt that was blocked.
    related: tuple[BundleRecord, ...]
    # When the Mandate was revoked, if that happened before the Dispute was opened. The
    # snapshot shows the Mandate as the Decision saw it, so a later Revocation is here.
    mandate_revoked_at: AwareDatetime | None = None
    # The `captured` payment records of the Payer's other Decisions at this merchant,
    # under any Mandate, before the Dispute was opened: what a duplicate is checked against.
    payer_payments: tuple[BundleRecord, ...] = ()

    def cites(self) -> frozenset[AuditRecordId]:
        """Every Audit record a claim may cite."""
        records = (*self.trail, *self.related, *self.payer_payments)
        return frozenset(r.audit_record_id for r in records)

    def hash(self) -> str:
        return "sha256:" + hashlib.sha256(canonical_json(self)).hexdigest()


class Assessment(_Frozen):
    """What the Policy pack's Dispute rules make of the bundle: worked out by code, never
    by a model."""

    reason: DisputeReason
    visa_code: str
    mastercard_code: str
    strength: EvidenceStrength
    # The facts that hold, and the facts the reason needs for strong evidence that don't.
    facts: tuple[EvidenceFact, ...]
    missing: tuple[EvidenceFact, ...]
    notes: tuple[str, ...] = ()


# What a claim is about, which fixes the kinds of Audit record it may cite.
ClaimKind = Literal[
    "mandate", "decision", "payer_approval", "payment", "void", "dispute", "context"
]

CLAIM_CITES: dict[ClaimKind, frozenset[AuditKind]] = {
    "mandate": frozenset({"decision"}),  # the Mandate snapshot is in the decision record
    "decision": frozenset({"decision"}),
    "payer_approval": frozenset({"consent_resolution"}),
    "payment": frozenset({"payment"}),
    "void": frozenset({"void"}),
    "dispute": frozenset({"dispute_opened"}),
    "context": frozenset({"decision", "payment"}),  # related Decisions, the Payer's Payments
}


class Claim(_Frozen):
    text: str = Field(min_length=1)
    cites: tuple[AuditRecordId, ...] = Field(min_length=1)
    # Optional so packs written before kinds existed still parse.
    kind: ClaimKind | None = None


NarrativeSource = Literal["template", "model"]

# Why the checker refused a model's narrative.
CheckCode = Literal[
    "stopped",  # the reply didn't finish cleanly (max_tokens, refusal)
    "unparseable",
    "empty",
    "unknown_kind",
    "uncited",
    "not_in_bundle",
    "wrong_kind",
]


class CheckFailure(_Frozen):
    code: CheckCode
    # The claim it is about, counted from 0; None for the reply as a whole.
    claim: int | None = None
    detail: str = ""


class NarrativeAttempt(_Frozen):
    """One model call for a narrative and what the checker made of it. `error` is set
    instead of an output when the call itself failed."""

    model: str
    prompt_version: str
    raw_output: str | None
    error: str | None = None
    failures: tuple[CheckFailure, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd_micros: int = 0

    @property
    def accepted(self) -> bool:
        return self.error is None and not self.failures


class EvidencePack(_Frozen):
    dispute_id: DisputeId
    version: int = Field(ge=1)
    bundle: EvidenceBundle
    bundle_hash: str
    narrative: tuple[Claim, ...]
    narrative_source: NarrativeSource
    built_at: AwareDatetime
    # The Policy pack active when this version was built, and what its Dispute rules made
    # of the bundle. No assessment from a pack without Dispute rules.
    policy_pack: PackRef | None = None
    assessment: Assessment | None = None
    # Every model call made for this version's narrative, and why a template was used
    # instead ("model narrative unavailable"), if it was.
    narrative_attempts: tuple[NarrativeAttempt, ...] = ()
    narrative_note: str | None = None

    @model_validator(mode="after")
    def _cited_from_the_bundle(self) -> Self:
        if self.bundle_hash != self.bundle.hash():
            raise ValueError("the bundle hash does not match the bundle")
        allowed = self.bundle.cites()
        for claim in self.narrative:
            stray = [c for c in claim.cites if c not in allowed]
            if stray:
                raise ValueError(f"a claim cites records not in the bundle: {stray}")
        return self

    def body(self) -> bytes:
        return canonical_json(self)

    def hash(self) -> str:
        """The pack hash: what a Sign-off approves and the Audit chain records."""
        return "sha256:" + hashlib.sha256(self.body()).hexdigest()
