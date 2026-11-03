"""Checking a Dispute's Evidence packs against the Audit chain (`admin verify-pack`).

The bundle is gathered by fixed rules from the chain up to the Dispute's
`dispute_opened` record (ADR-0018), so it can be gathered again at any time and must
come out the same. For every stored pack version this checks that:

- the stored bytes still parse as a pack whose `bundle_hash` is its bundle's hash;
- that bundle is the one the chain gives now;
- the assessment re-derives identically with the Policy pack the version recorded;
- a narrative written by code is the one code writes for that bundle;
- a signed-off version's stored bytes hash to the pack hash its Sign-off recorded.

A model's narrative can't be written again, so it is held to its Sign-off's pack hash,
and every claim must cite records in the bundle, which parsing the pack checks.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from core.domain.disputes import DisputeId
from core.domain.evidence import EvidencePack
from core.evidence import gather_bundle, template_narrative
from core.evidence_strength import assess
from core.policy.packs import PackError, load_pack
from core.ports import TenantId, UnitOfWork


@dataclass
class VersionCheck:
    version: int
    problems: list[str] = field(default_factory=list)
    signed_off: bool = False


@dataclass
class PackReport:
    versions: list[VersionCheck]
    # A Policy pack a version recorded can't be loaded, so its assessment can't be checked.
    pack_problems: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not any(v.problems for v in self.versions)


def verify_pack(
    store: UnitOfWork, tenant_id: TenantId, dispute_id: DisputeId, packs_dir: Path
) -> PackReport | None:
    """None if the Tenant has no such Dispute."""
    with store.transaction(tenant_id) as tx:
        dispute = tx.get_dispute(dispute_id)
        if dispute is None:
            return None
        bodies = tx.evidence_pack_bodies(dispute_id)
        rebuilt = gather_bundle(tx, dispute)
        records = tx.audit_records_for({dispute.decision_id}, 2**62)
    signed = {
        s.record.sign_off.pack_version: s.record.sign_off.pack_hash
        for s in records
        if s.record.sign_off is not None and s.record.sign_off.dispute_id == dispute_id
    }
    report = PackReport(versions=[])
    for version, body in bodies:
        check = VersionCheck(version=version, signed_off=version in signed)
        report.versions.append(check)
        if version in signed and _sha256(body) != signed[version]:
            check.problems.append("differs from the pack hash its Sign-off recorded")
        try:
            pack = EvidencePack.model_validate_json(body)
        except ValidationError as exc:
            check.problems.append(f"can't be read: {exc.errors()[0]['msg']}")
            continue
        if pack.bundle_hash != rebuilt.hash():
            check.problems.append("its bundle differs from the one the Audit chain gives")
        if pack.narrative_source == "template" and pack.narrative != template_narrative(rebuilt):
            check.problems.append("the narrative written by code differs")
        ref = pack.policy_pack
        if ref is None or pack.assessment is None:
            continue
        try:
            rules = load_pack(packs_dir, ref.id, ref.version).pack.disputes
        except PackError as exc:
            report.pack_problems.append(f"Pack v{version}: {exc}")
            continue
        if rules is None or assess(rebuilt, pack.assessment.reason, rules) != pack.assessment:
            check.problems.append(
                f"the assessment differs from {ref.id}@{ref.version}'s Dispute rules"
            )
    return report


def _sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()
