"""The `step_up_consent` workflow (ADR-0009).

Postgres owns consent state; this workflow only orchestrates. It notifies, then waits
until the Consent request is resolved (a signal) or its expiry passes (a durable timer).
On the timer it asks the expire activity to write the `expired` resolution, which writes
nothing if the request was resolved meanwhile. The workflow ID is the Consent request
ID, so a repeated start is refused and the relay can retry safely.

This module runs inside Temporal's workflow sandbox: it imports only the standard library
and temporalio, and names its activities as strings so the store never loads here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

WORKFLOW_NAME = "step_up_consent"
NOTIFY = "notify_consent_requested"
EXPIRE = "expire_consent_request"

# The expire activity refuses to run before expiry by the store's clock, so a timer that
# fires a little early is retried until the request is due.
_ACTIVITY_RETRY = RetryPolicy(maximum_interval=timedelta(minutes=1))
_ACTIVITY_TIMEOUT = timedelta(seconds=30)


@dataclass(frozen=True)
class ConsentArgs:
    tenant_id: str
    consent_request_id: str
    expires_at: str  # ISO 8601 UTC


@workflow.defn(name=WORKFLOW_NAME)
class StepUpConsent:
    def __init__(self) -> None:
        self._resolved = False

    @workflow.run
    async def run(self, args: ConsentArgs) -> str:
        """Returns `resolved` if signalled, else what the expire activity did."""
        await workflow.execute_activity(
            NOTIFY, args, start_to_close_timeout=_ACTIVITY_TIMEOUT, retry_policy=_ACTIVITY_RETRY
        )
        remaining = datetime.fromisoformat(args.expires_at) - workflow.now()
        if remaining > timedelta(0) and not self._resolved:
            try:
                await workflow.wait_condition(lambda: self._resolved, timeout=remaining)
            except TimeoutError:
                pass
        if self._resolved:
            return "resolved"
        outcome: str = await workflow.execute_activity(
            EXPIRE,
            args,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
            result_type=str,
        )
        return outcome

    @workflow.signal(name="resolved")
    def resolved(self) -> None:
        """The API stored an approval or decline; stop waiting."""
        self._resolved = True
