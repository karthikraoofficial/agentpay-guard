"""Writing a Consent resolution (ADR-0009): one Audit record in the Tenant's chain plus
one row for lookup, in the caller's transaction. Shared by the API (approve, decline)
and the consent workflow's expiry activity."""

from datetime import datetime

from core.domain.audit import AuditRecord, DecisionInputs, seal
from core.domain.consent import (
    Attestation,
    ConsentRequest,
    ConsentResolution,
    ResolutionEvidence,
    ResolutionStatus,
    ResolvedVia,
)
from core.domain.decisions import AuditRecordId, EngineOutput, PackRef
from core.domain.parties import new_id
from core.ports import TenantTransaction
from core.tenant_events import consent_request_resolved


def record_resolution(
    tx: TenantTransaction,
    consent: ConsentRequest,
    status: ResolutionStatus,
    via: ResolvedVia,
    attestation: Attestation | None,
    now: datetime,
    *,
    run: tuple[DecisionInputs, PackRef, EngineOutput] | None = None,
) -> ConsentResolution:
    """Append the resolution's Audit record and store it for lookup. The caller holds
    the Consent request's lock; this takes the chain head. `run` is an approval's engine
    re-run, recorded so the resolution replays like a Decision."""
    inputs, ref, output = run if run else (None, None, None)
    seq, prev_hash = tx.lock_chain_head()
    resolution = ConsentResolution(
        consent_request_id=consent.consent_request_id,
        tenant_id=consent.tenant_id,
        decision_id=consent.decision_id,
        status=status,
        resolved_at=now,
        resolved_via=via,
        attestation=attestation,
        audit_record_id=AuditRecordId(new_id("aud")),
        reason_codes=output.reason_codes if output and status == "blocked" else None,
    )
    sealed = seal(
        AuditRecord(
            audit_record_id=resolution.audit_record_id,
            tenant_id=consent.tenant_id,
            seq=seq + 1,
            prev_hash=prev_hash,
            kind="consent_resolution",
            decision_id=consent.decision_id,
            recorded_at=now,
            inputs=inputs,
            pack=ref,
            output=output,
            resolution=ResolutionEvidence(
                consent_request_id=consent.consent_request_id,
                status=status,
                resolved_via=via,
                attestation=attestation,
            ),
        )
    )
    tx.add_consent_resolution(resolution)
    tx.append_audit_record(sealed)
    tx.publish(consent_request_resolved(resolution))
    return resolution
