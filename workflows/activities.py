"""Activities for `step_up_consent`. Both are safe to run more than once: Temporal retries
an activity whose worker died, so a repeat must never write a second record."""

import logging

from temporalio import activity
from temporalio.exceptions import ApplicationError

from core.domain.consent import ConsentRequestId
from core.domain.decisions import decision_time
from core.ports import Clock, TenantId, UnitOfWork
from core.resolutions import record_resolution
from workflows.consent import EXPIRE, NOTIFY, ConsentArgs

log = logging.getLogger(__name__)


class ConsentActivities:
    def __init__(self, store: UnitOfWork, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    @activity.defn(name=NOTIFY)
    def notify(self, args: ConsentArgs) -> None:
        """Tell the Tenant a Consent request is waiting. Phase 3: one structured log line,
        keyed by the Consent request ID so a repeat can be recognised. Identifiers and
        times only: never a Consent link token or contact details."""
        tenant_id = TenantId(args.tenant_id)
        with self._store.transaction(tenant_id) as tx:
            consent = tx.find_consent_request(ConsentRequestId(args.consent_request_id))
        if consent is None:
            log.warning("consent_request.missing consent_request_id=%s", args.consent_request_id)
            return
        log.info(
            "consent_request.created tenant_id=%s consent_request_id=%s decision_id=%s "
            "expires_at=%s",
            consent.tenant_id,
            consent.consent_request_id,
            consent.decision_id,
            consent.expires_at.isoformat(),
            extra={
                "event": "consent_request.created",
                "tenant_id": consent.tenant_id,
                "consent_request_id": consent.consent_request_id,
                "decision_id": consent.decision_id,
                "expires_at": consent.expires_at.isoformat(),
            },
        )

    @activity.defn(name=EXPIRE)
    def expire(self, args: ConsentArgs) -> str:
        """Write the `expired` resolution unless the request was resolved first. Returns
        `expired`, `already_resolved` or `missing`. Before expiry by this clock it raises,
        so Temporal retries it once the request is due."""
        now = decision_time(self._clock.now())
        with self._store.transaction(TenantId(args.tenant_id)) as tx:
            consent = tx.lock_consent_request(ConsentRequestId(args.consent_request_id))
            if consent is None:
                return "missing"
            if consent.resolution is not None:
                return "already_resolved"
            if now < consent.expires_at:
                raise ApplicationError(
                    f"Consent request {consent.consent_request_id} is not due yet",
                    type="NotYetExpired",
                )
            record_resolution(tx, consent, "expired", "timer", None, now)
        return "expired"
