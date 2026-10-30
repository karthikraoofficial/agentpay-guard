"""The consent worker: `uv run python -m workflows.worker`.

One process hosts the `step_up_consent` workflow, its activities, and the outbox relay
that starts a workflow for every committed step_up (ADR-0009). Stop it with Ctrl+C;
killing it outright is safe too, since Temporal resumes every waiting workflow on the
next worker from its history.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine
from temporalio.client import Client
from temporalio.worker import Worker

from adapters.postgres.store import PostgresStore
from adapters.system_clock import SystemClock
from core.ports import Clock, EventBus, UnitOfWork
from core.relay import relay_once
from core.settings import get_settings
from workflows.activities import ConsentActivities
from workflows.client import TemporalWorkflow
from workflows.consent import StepUpConsent

log = logging.getLogger(__name__)


def consent_worker(
    client: Client, task_queue: str, store: UnitOfWork, clock: Clock, pool: ThreadPoolExecutor
) -> Worker:
    activities = ConsentActivities(store, clock)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[StepUpConsent],
        activities=[activities.notify, activities.expire],
        activity_executor=pool,
    )


async def relay_forever(
    bus: EventBus, client: Client, task_queue: str, clock: Clock, interval_seconds: float
) -> None:
    """Deliver outbox events until cancelled. The relay is synchronous, so each pass runs
    in a thread and reaches this loop's client through the port."""
    port = TemporalWorkflow.for_client(client, asyncio.get_running_loop(), task_queue)
    while True:
        delivered = await asyncio.to_thread(relay_once, bus, port, clock)
        if delivered:
            log.info("outbox relay delivered %d event(s)", delivered)
        await asyncio.sleep(interval_seconds)


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = PostgresStore(create_engine(str(settings.database_url), pool_pre_ping=True))
    clock = SystemClock()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    with ThreadPoolExecutor(max_workers=8) as pool:
        worker = consent_worker(client, settings.temporal_task_queue, store, clock, pool)
        async with worker:
            log.info("consent worker polling task queue %s", settings.temporal_task_queue)
            await relay_forever(
                store,
                client,
                settings.temporal_task_queue,
                clock,
                settings.outbox_poll_interval_ms / 1000,
            )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
