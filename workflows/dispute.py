"""The `dispute_pack` workflow (ADR-0018).

Postgres owns Dispute state; this workflow only orchestrates. It asks the build activity
for the next Evidence pack version, then waits until the Dispute is closed (signed off or
withdrawn: a signal), a Regenerate is asked for (a signal: build again), or `respond_by`
passes (a durable timer: the expire activity expires it unless it was closed first).

The build activity is driven by the Dispute's status, so a lost or repeated signal does no
harm: a build of a closed Dispute writes nothing, and one already in review is a no-op.
The workflow ID is the Dispute ID, so a repeated start is refused and the relay can retry
safely.

This module runs inside Temporal's workflow sandbox: it imports only the standard library
and temporalio, and names its activities as strings so the store never loads here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

WORKFLOW_NAME = "dispute_pack"
BUILD = "build_evidence_pack"
EXPIRE = "expire_dispute"

# The expire activity refuses to run before `respond_by` by the store's clock, so a timer
# that fires a little early is retried until the Dispute is due.
_ACTIVITY_RETRY = RetryPolicy(maximum_interval=timedelta(minutes=1))
# A build may make two model calls (a narrative and one retry) at up to 45 s each.
_ACTIVITY_TIMEOUT = timedelta(minutes=3)


@dataclass(frozen=True)
class DisputeArgs:
    tenant_id: str
    dispute_id: str
    respond_by: str  # ISO 8601 UTC


@workflow.defn(name=WORKFLOW_NAME)
class DisputePack:
    def __init__(self) -> None:
        self._closed = False
        self._regenerations = 0  # asked for so far
        self._handled = 0  # built for so far

    @workflow.run
    async def run(self, args: DisputeArgs) -> str:
        """Returns `closed` once signed off or withdrawn, else what the expire activity
        did, or `missing`."""
        while True:
            built = await self._activity(BUILD, args)
            if built in ("closed", "missing"):
                return built
            remaining = datetime.fromisoformat(args.respond_by) - workflow.now()
            if not self._waiting_ended() and remaining > timedelta(0):
                try:
                    await workflow.wait_condition(self._waiting_ended, timeout=remaining)
                except TimeoutError:
                    pass
            if self._closed:
                return "closed"
            if self._regenerations == self._handled:  # the deadline passed
                return await self._activity(EXPIRE, args)
            self._handled = self._regenerations

    def _waiting_ended(self) -> bool:
        return self._closed or self._regenerations > self._handled

    async def _activity(self, name: str, args: DisputeArgs) -> str:
        outcome: str = await workflow.execute_activity(
            name,
            args,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
            result_type=str,
        )
        return outcome

    @workflow.signal(name="regenerate")
    def regenerate(self) -> None:
        """The API asked for the next pack version; build it."""
        self._regenerations += 1

    @workflow.signal(name="closed")
    def closed(self) -> None:
        """The API stored a Sign-off or a withdrawal; stop waiting."""
        self._closed = True
