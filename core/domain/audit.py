"""Audit records and the per-Tenant Audit chain (ADR-0004).

An Audit record is stored as its exact canonical JSON bytes. Its hash is
SHA-256(previous record's 32-byte hash || those bytes), and a Tenant's first record
uses 32 zero bytes as the previous hash. Any edit, deletion or insertion breaks the chain.
"""

import hashlib
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

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
