"""Workflow fakes: one that records calls for tests, and one for when no Temporal adapter
is wired in."""

from datetime import datetime

from core.domain.consent import ConsentRequestId
from core.ports import TenantId, Workflow


class WorkflowUnavailable(Exception):
    """The workflow side can't be reached; the caller should try again later."""


class InMemoryWorkflow:
    """Records starts and signals. A start for a Consent request already started is a
    no-op, as with Temporal workflow IDs. Tests can make either call fail."""

    def __init__(self) -> None:
        self.started: dict[ConsentRequestId, tuple[TenantId, datetime]] = {}
        self.start_calls = 0
        self.signals: list[tuple[TenantId, ConsentRequestId]] = []
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


class UnconfiguredWorkflow:
    """Used until a Temporal adapter is wired in: signals are dropped, since Postgres owns
    consent state, and starts fail so the relay keeps the outbox event for later."""

    def start_consent(
        self, tenant_id: TenantId, consent_request_id: ConsentRequestId, expires_at: datetime
    ) -> None:
        raise WorkflowUnavailable("no workflow adapter is configured")

    def signal_resolved(self, tenant_id: TenantId, consent_request_id: ConsentRequestId) -> None:
        return None


_conforms: type[Workflow] = InMemoryWorkflow
_conforms_unconfigured: type[Workflow] = UnconfiguredWorkflow
