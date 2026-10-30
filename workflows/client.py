"""The Workflow port on Temporal.

The port is synchronous (FastAPI routes and the relay are), and the Temporal client is
asynchronous, so calls run on an event loop of the adapter's own, in a background
thread, unless the caller hands in a client and the loop it lives on. The client
connects on first use, so building the app never waits on Temporal.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from core.domain.consent import ConsentRequestId
from core.ports import TenantId, Workflow
from core.settings import Settings
from workflows.consent import ConsentArgs, StepUpConsent


class TemporalWorkflow:
    def __init__(
        self,
        address: str,
        namespace: str,
        task_queue: str,
        *,
        client: Client | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._address = address
        self._namespace = namespace
        self._task_queue = task_queue
        self._client = client
        self._loop = loop
        self._timeout = timeout_seconds
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "TemporalWorkflow":
        return cls(
            settings.temporal_address, settings.temporal_namespace, settings.temporal_task_queue
        )

    @classmethod
    def for_client(
        cls, client: Client, loop: asyncio.AbstractEventLoop, task_queue: str
    ) -> "TemporalWorkflow":
        """Use an already connected client on `loop`. Call the port from another thread."""
        return cls("", client.namespace, task_queue, client=client, loop=loop)

    def start_consent(
        self, tenant_id: TenantId, consent_request_id: ConsentRequestId, expires_at: datetime
    ) -> None:
        args = ConsentArgs(
            tenant_id=tenant_id,
            consent_request_id=consent_request_id,
            expires_at=expires_at.astimezone(UTC).isoformat(),
        )

        async def start(client: Client) -> None:
            try:
                await client.start_workflow(
                    StepUpConsent.run,
                    args,
                    id=consent_request_id,
                    task_queue=self._task_queue,
                    # A finished workflow is never started again for the same request.
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                return

        self._call(start)

    def signal_resolved(self, tenant_id: TenantId, consent_request_id: ConsentRequestId) -> None:
        async def signal(client: Client) -> None:
            handle: WorkflowHandle[StepUpConsent, str] = client.get_workflow_handle_for(
                StepUpConsent.run, consent_request_id
            )
            await handle.signal(StepUpConsent.resolved)

        self._call(signal)

    def _call(self, action: Callable[[Client], Awaitable[None]]) -> None:
        loop = self._ensure_loop()

        async def run() -> None:
            await action(await self._connected())

        asyncio.run_coroutine_threadsafe(run(), loop).result(timeout=self._timeout)

    async def _connected(self) -> Client:
        if self._client is None:
            self._client = await Client.connect(self._address, namespace=self._namespace)
        return self._client

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(
                    target=loop.run_forever, name="temporal-client", daemon=True
                ).start()
                self._loop = loop
            return self._loop


_conforms: type[Workflow] = TemporalWorkflow
