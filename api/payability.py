"""`payable_until`: the end of a Decision's Payment window, worked out at the API edge.

An allow can be paid until its Decision time plus the window of the pack it was decided
with. An approved step-up can be paid until its Consent resolution time plus the window
of the pack the approval was re-checked with (ADR-0010). Anything else has no window.
The engine never sees this, and no Audit record carries it."""

from datetime import datetime, timedelta
from pathlib import Path

from core.domain.consent import ConsentRequest
from core.domain.decisions import Decision, PackRef
from core.policy.packs import LoadedPack, load_pack
from core.ports import TenantTransaction


def payment_window(ref: PackRef, active: LoadedPack, packs_dir: Path) -> timedelta:
    """The Payment window of the released pack `ref` names."""
    if (ref.id, ref.version) == (active.pack.id, active.pack.version):
        return active.pack.payment_window
    return load_pack(packs_dir, ref.id, ref.version).pack.payment_window


def payable_until(
    tx: TenantTransaction,
    decision: Decision,
    consent: ConsentRequest | None,
    active: LoadedPack,
    packs_dir: Path,
) -> datetime | None:
    if decision.outcome == "allow":
        return decision.decided_at + payment_window(decision.policy_pack, active, packs_dir)
    resolution = consent.resolution if consent else None
    if resolution is None or resolution.status != "approved":
        return None
    sealed = tx.get_audit_record(resolution.audit_record_id)
    ref = sealed.record.pack if sealed else None
    if ref is None:
        raise RuntimeError(f"Approval {resolution.audit_record_id} has no recorded pack")
    return resolution.resolved_at + payment_window(ref, active, packs_dir)
