"""Checking what the Audit chain holds: Replay one record and verify a Tenant's chain. The
one implementation behind `api.admin replay`, `api.admin verify` and the inspector's read
endpoints (#275)."""

import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.domain.audit import AuditRecord, SealedAuditRecord, first_broken_seq
from core.policy.packs import PackError, load_pack
from core.policy.replay import PackMismatch, replay
from core.ports import TenantTransaction

ReplayStatus = Literal["identical", "different", "nothing_to_replay", "pack_problem"]


@dataclass(frozen=True)
class ReplayReport:
    audit_record_id: str
    seq: int
    status: ReplayStatus
    # A unified diff of the recorded and replayed output, when they differ.
    difference: str | None = None
    # What kind of record has no engine run, or why the recorded pack can't be used.
    detail: str | None = None

    @property
    def identical(self) -> bool:
        return self.status == "identical"


def replay_record(sealed: SealedAuditRecord, packs_dir: Path) -> ReplayReport:
    """Re-run the record's engine run on its recorded pack and compare the bytes."""
    audit_record_id, seq = sealed.record.audit_record_id, sealed.seq
    recorded = sealed.record.pack
    if recorded is None:
        return ReplayReport(
            audit_record_id, seq, "nothing_to_replay", detail=_no_engine_run(sealed.record)
        )
    try:
        loaded = load_pack(packs_dir, recorded.id, recorded.version)
        result = replay(sealed.body, loaded.pack, loaded.hash)
    except (PackError, PackMismatch) as exc:
        return ReplayReport(audit_record_id, seq, "pack_problem", detail=str(exc))
    if result.identical:
        return ReplayReport(audit_record_id, seq, "identical")
    difference = "".join(
        difflib.unified_diff(
            _lines(result.recorded), _lines(result.replayed), "recorded", "replayed"
        )
    )
    return ReplayReport(audit_record_id, seq, "different", difference=difference)


@dataclass(frozen=True)
class ChainReport:
    record_count: int
    first_broken_seq: int | None

    @property
    def clean(self) -> bool:
        return self.first_broken_seq is None


def verify_tenant_chain(tx: TenantTransaction) -> ChainReport:
    """Walk this Tenant's whole Audit chain."""
    rows, head = tx.read_audit_chain()
    return ChainReport(len(rows), first_broken_seq(rows, head))


def _no_engine_run(record: AuditRecord) -> str:
    if record.payment is not None:
        return f"{record.payment.status} Payment record"
    if record.void is not None:
        return "Void record"
    if record.dispute is not None:
        return "Dispute record"
    if record.sign_off is not None:
        return "Sign-off record"
    status = record.resolution.status if record.resolution else record.kind
    return f"{status} Consent resolution"


def _lines(canonical: bytes) -> list[str]:
    return (json.dumps(json.loads(canonical), indent=2, sort_keys=True) + "\n").splitlines(True)
