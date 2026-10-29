"""Audit records and the per-Tenant Audit chain (ADR-0004).

An Audit record is stored as its exact canonical JSON bytes. Its hash is
SHA-256(previous record's 32-byte hash || those bytes), and a Tenant's first record
uses 32 zero bytes as the previous hash. Any edit, deletion or insertion breaks the chain.
"""

import hashlib
from collections.abc import Sequence
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from core.domain.canonical_json import canonical_json
from core.domain.decisions import (
    AgentPaymentRequest,
    AuditRecordId,
    DecisionId,
    EngineOutput,
    PackRef,
)
from core.domain.mandates import Mandate
from core.domain.money import Money
from core.domain.parties import Agent, TenantId

AuditKind = Literal["decision", "consent_resolution", "void"]
ZERO_HASH = "sha256:" + "00" * 32


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DecisionInputs(_Frozen):
    """Everything the engine was given. Replay feeds these back in unchanged."""

    request: AgentPaymentRequest
    mandate: Mandate
    agent: Agent | None
    spent_this_month: Money
    now: AwareDatetime


class AuditRecord(_Frozen):
    audit_record_id: AuditRecordId
    tenant_id: TenantId
    seq: int = Field(ge=1)
    prev_hash: str
    kind: AuditKind
    decision_id: DecisionId
    recorded_at: AwareDatetime
    inputs: DecisionInputs
    pack: PackRef
    signals: tuple[dict[str, Any], ...] = ()
    output: EngineOutput


class SealedAuditRecord(_Frozen):
    """An Audit record with the exact bytes that were hashed and stored."""

    record: AuditRecord
    body: bytes
    record_hash: str

    @property
    def seq(self) -> int:
        return self.record.seq

    @property
    def prev_hash(self) -> str:
        return self.record.prev_hash


def chain_hash(prev_hash: str, body: bytes) -> str:
    prev = bytes.fromhex(prev_hash.removeprefix("sha256:"))
    return "sha256:" + hashlib.sha256(prev + body).hexdigest()


def seal(record: AuditRecord) -> SealedAuditRecord:
    body = canonical_json(record)
    return SealedAuditRecord(
        record=record, body=body, record_hash=chain_hash(record.prev_hash, body)
    )


def unseal(body: bytes, record_hash: str) -> SealedAuditRecord:
    """Rebuild a stored record from its bytes, without re-serialising anything."""
    return SealedAuditRecord(
        record=AuditRecord.model_validate_json(body), body=body, record_hash=record_hash
    )


class StoredAuditRow(_Frozen):
    """An Audit record exactly as stored: the hashed bytes plus the columns kept beside
    them for lookup. Verification checks the columns against the bytes too."""

    seq: int
    audit_record_id: str
    kind: str
    decision_id: str
    body: bytes
    prev_hash: str
    record_hash: str

    @classmethod
    def from_sealed(cls, sealed: SealedAuditRecord) -> Self:
        r = sealed.record
        return cls(
            seq=r.seq,
            audit_record_id=r.audit_record_id,
            kind=r.kind,
            decision_id=r.decision_id,
            body=sealed.body,
            prev_hash=r.prev_hash,
            record_hash=sealed.record_hash,
        )


def first_broken_seq(rows: Sequence[StoredAuditRow], head: tuple[int, str]) -> int | None:
    """Walk a Tenant's chain in seq order and return the first seq that is missing,
    edited or out of place, or None if the whole chain and its head check out."""
    prev, expected = ZERO_HASH, 1
    for row in sorted(rows, key=lambda r: r.seq):
        if row.seq != expected:
            return expected
        if row.prev_hash != prev or chain_hash(row.prev_hash, row.body) != row.record_hash:
            return row.seq
        try:
            record = AuditRecord.model_validate_json(row.body)
        except ValidationError:
            return row.seq
        columns = (row.seq, row.prev_hash, row.audit_record_id, row.kind, row.decision_id)
        if columns != (
            record.seq,
            record.prev_hash,
            record.audit_record_id,
            record.kind,
            record.decision_id,
        ):
            return row.seq
        prev, expected = row.record_hash, expected + 1
    last = expected - 1
    if head == (last, prev):
        return None
    return expected if head[0] > last else max(last, 1)
