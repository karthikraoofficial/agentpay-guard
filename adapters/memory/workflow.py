"""Workflow fakes: one that records calls for tests, and one for when no Temporal adapter
is wired in."""

from datetime import datetime

from core.domain.consent import ConsentRequestId
from core.domain.disputes import DisputeId
from core.ports import DisputeSignal, TenantId, Workflow


class WorkflowUnavailable(Exception):
    """The workflow side can't be reached; the caller should try again later."""


class InMemoryWorkflow:
    """Records starts and signals. A start for a Consent request already started is a
    no-op, as with Temporal workflow IDs. Tests can make either call fail."""

    def __init__(self) -> None:
        self.started: dict[ConsentRequestId, tuple[TenantId, datetime]] = {}
        self.start_calls = 0
        self.signals: list[tuple[TenantId, ConsentRequestId]] = []
        self.dispute_starts: dict[DisputeId, TenantId] = {}
        self.dispute_signals: list[tuple[TenantId, DisputeId, DisputeSignal]] = []
        self.fail_starts = False
        self.fail_signals = False

    def start_consent(
        self, tenant_id: TenantId, consent_request_id: ConsentRequestId, expires_at: datetime
    ) -> None:
        self.start_calls += 1
        if self.fail_starts:
            raise WorkflowUnavailable("start refused by the test")
        self.started.setdefault(consent_request_id, (tenant_id, expires_at))

    def signal_resolved(self, tenant_id: TenantId, consent_request_id: ConsentRequestId) -> None:
        if self.fail_signals:
            raise WorkflowUnavailable("signal refused by the test")
        self.signals.append((tenant_id, consent_request_id))

    def start_dispute_pack(
        self, tenant_id: TenantId, dispute_id: DisputeId, respond_by: datetime
    ) -> None:
        self.start_calls += 1
        if self.fail_starts:
            raise WorkflowUnavailable("start refused by the test")
        self.dispute_starts.setdefault(dispute_id, tenant_id)

    def signal_dispute(
        self, tenant_id: TenantId, dispute_id: DisputeId, signal: DisputeSignal
    ) -> None:
        if self.fail_signals:
            raise WorkflowUnavailable("signal refused by the test")
        self.dispute_signals.append((tenant_id, dispute_id, signal))


class UnconfiguredWorkflow:
    """Used until a Temporal adapter is wired in: signals are dropped, since Postgres owns
    consent state, and starts fail so the relay keeps the outbox event for later."""

    def start_consent(
        self, tenant_id: TenantId, consent_request_id: ConsentRequestId, expires_at: datetime
    ) -> None:
        raise WorkflowUnavailable("no workflow adapter is configured")

    def signal_resolved(self, tenant_id: TenantId, consent_request_id: ConsentRequestId) -> None:
        return None

    def start_dispute_pack(
        self, tenant_id: TenantId, dispute_id: DisputeId, respond_by: datetime
    ) -> None:
        raise WorkflowUnavailable("no workflow adapter is configured")

    def signal_dispute(
        self, tenant_id: TenantId, dispute_id: DisputeId, signal: DisputeSignal
    ) -> None:
        raise WorkflowUnavailable("no workflow adapter is configured")


_conforms: type[Workflow] = InMemoryWorkflow
_conforms_unconfigured: type[Workflow] = UnconfiguredWorkflow
