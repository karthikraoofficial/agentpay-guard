"""The `dispute_pack` workflow (ADR-0018).

Postgres owns Dispute state; this workflow only orchestrates. It asks the build activity
to gather the Evidence bundle and store the first pack, which puts the Dispute in review.
The workflow ID is the Dispute ID, so a repeated start is refused and the relay can retry
safely.

This module runs inside Temporal's workflow sandbox: it imports only the standard library
and temporalio, and names its activity as a string so the store never loads here.
"""

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

WORKFLOW_NAME = "dispute_pack"
BUILD = "build_evidence_pack"

_ACTIVITY_RETRY = RetryPolicy(maximum_interval=timedelta(minutes=1))
_ACTIVITY_TIMEOUT = timedelta(seconds=60)


@dataclass(frozen=True)
class DisputeArgs:
    tenant_id: str
    dispute_id: str


@workflow.defn(name=WORKFLOW_NAME)
class DisputePack:
    @workflow.run
    async def run(self, args: DisputeArgs) -> str:
        """Returns what the build activity did: `built`, `already_built` or `missing`."""
        outcome: str = await workflow.execute_activity(
            BUILD,
            args,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
            result_type=str,
        )
        return outcome
