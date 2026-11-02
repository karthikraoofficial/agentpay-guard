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
from core.domain.decisions import AuditRecordId
from core.domain.disputes import DisputeId
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

    def cites(self) -> frozenset[AuditRecordId]:
        """Every Audit record a claim may cite."""
        return frozenset(r.audit_record_id for r in (*self.trail, *self.related))

    def hash(self) -> str:
        return "sha256:" + hashlib.sha256(canonical_json(self)).hexdigest()


class Claim(_Frozen):
    text: str = Field(min_length=1)
    cites: tuple[AuditRecordId, ...] = Field(min_length=1)


NarrativeSource = Literal["template", "model"]


class EvidencePack(_Frozen):
    dispute_id: DisputeId
    version: int = Field(ge=1)
    bundle: EvidenceBundle
    bundle_hash: str
    narrative: tuple[Claim, ...]
    narrative_source: NarrativeSource
    built_at: AwareDatetime

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
