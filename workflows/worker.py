"""The consent worker: `uv run python -m workflows.worker`.

One process hosts the `step_up_consent` and `dispute_pack` workflows, their activities,
the outbox relay that starts a workflow for every committed step_up and Dispute
(ADR-0009), and the reconciliation sweep that settles Payments whose rail webhook never
came (#82). Stop it with Ctrl+C;
killing it outright is safe too, since Temporal resumes every waiting workflow on the
next worker from its history.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine
from temporalio.client import Client
from temporalio.worker import Worker

from adapters.gateways import build_gateway
from adapters.memory.settling_rail import SettlingMockRail
from adapters.otel.telemetry import OtelTelemetry
from adapters.postgres.store import PostgresStore
from adapters.rails import build_rail
from adapters.system_clock import SystemClock
from adapters.webhooks.secret_box import secret_box
from adapters.webhooks.sender import HttpWebhookSender
from agents.dispute_narrative import NarrativeWriter as DisputeNarrativeWriter
from core.debits import run_debit_step
from core.evidence import NarrativeWriter
from core.ports import Clock, EventBus, PaymentRail, UnitOfWork
from core.reconcile import SweepStore, sweep
from core.relay import relay_once
from core.settings import Settings, get_settings
from core.tenant_webhooks import TenantWebhooks
from workflows.activities import ConsentActivities, DisputeActivities
from workflows.client import TemporalWorkflow
from workflows.consent import StepUpConsent
from workflows.dispute import DisputePack

log = logging.getLogger(__name__)


# The released Policy packs, for workers that aren't given a directory (tests).
PACKS = Path(__file__).resolve().parents[1] / "policy_packs"


def consent_worker(
    client: Client,
    task_queue: str,
    store: UnitOfWork,
    clock: Clock,
    pool: ThreadPoolExecutor,
    packs_dir: Path = PACKS,
    writer: NarrativeWriter | None = None,
) -> Worker:
    activities = ConsentActivities(store, clock)
    disputes = DisputeActivities(store, clock, packs_dir, writer)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[StepUpConsent, DisputePack],
        activities=[activities.notify, activities.expire, disputes.build, disputes.expire],
        activity_executor=pool,
    )


def narrative_writer(settings: Settings) -> NarrativeWriter | None:
    """The dispute evidence agent, or None to have every narrative written by code."""
    if not settings.dispute_narrative_enabled:
        return None
    gateway = build_gateway(settings, OtelTelemetry.from_settings(settings))
    return DisputeNarrativeWriter(
        gateway, settings.dispute_narrative_model, settings.dispute_narrative_timeout_ms
    )


async def relay_forever(
    bus: EventBus,
    client: Client,
    task_queue: str,
    clock: Clock,
    webhooks: TenantWebhooks,
    interval_seconds: float,
) -> None:
    """Deliver outbox events until cancelled: workflow starts and Tenant webhooks. The
    relay is synchronous, so each pass runs in a thread and reaches this loop's client
    through the port."""
    port = TemporalWorkflow.for_client(client, asyncio.get_running_loop(), task_queue)
    while True:
        delivered = await asyncio.to_thread(relay_once, bus, port, clock, webhooks)
        if delivered:
            log.info("outbox relay delivered %d event(s)", delivered)
        await asyncio.sleep(interval_seconds)


def sweep_rail(settings: Settings, store: UnitOfWork) -> PaymentRail | None:
    """The rail the sweep asks, or None when there is nothing to ask: MockRail keeps its
    charges inside the API process, so a worker's own MockRail would know none of them
    and the sweep would wrongly fail every Payment. With mock settle on (ADR-0019), the
    sweep asks a rail that captures from the stored Payment instead."""
    if settings.rail == "mock":
        return None if settings.mock_settle_after_seconds is None else SettlingMockRail(store)
    return build_rail(settings)


def sweep_age(settings: Settings) -> timedelta:
    """How old a scheduled Payment is before the sweep asks about it."""
    if settings.mock_settle_after_seconds is not None:
        return timedelta(seconds=settings.mock_settle_after_seconds)
    return timedelta(seconds=settings.reconcile_after_seconds)


async def sweep_forever(
    store: SweepStore, rail: PaymentRail, clock: Clock, settings: Settings
) -> None:
    """Until cancelled, each interval: debit the Payments whose Pre-debit notice period
    has ended, then reconcile the scheduled ones. Both are synchronous, so each pass runs
    in a thread."""
    older_than = sweep_age(settings)
    while True:
        debits = await asyncio.to_thread(run_debit_step, store, rail, clock)
        if debits.checked:
            log.info("debit step checked %d Payment(s), %d error(s)", debits.checked, debits.errors)
        result = await asyncio.to_thread(sweep, store, rail, clock, older_than)
        if result.checked:
            log.info(
                "reconciliation sweep checked %d Payment(s), %d error(s)",
                result.checked,
                result.errors,
            )
        await asyncio.sleep(settings.reconcile_interval_seconds)


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = PostgresStore(create_engine(str(settings.database_url), pool_pre_ping=True))
    clock = SystemClock()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    with ThreadPoolExecutor(max_workers=8) as pool:
        worker = consent_worker(
            client,
            settings.temporal_task_queue,
            store,
            clock,
            pool,
            settings.policy_packs_dir,
            narrative_writer(settings),
        )
        async with worker:
            log.info("consent worker polling task queue %s", settings.temporal_task_queue)
            loops = [
                relay_forever(
                    store,
                    client,
                    settings.temporal_task_queue,
                    clock,
                    TenantWebhooks(store, secret_box(settings), HttpWebhookSender(), clock),
                    settings.outbox_poll_interval_ms / 1000,
                )
            ]
            rail = sweep_rail(settings, store)
            if rail is None:
                log.info("reconciliation sweep off: mock Payments stay scheduled (no mock settle)")
            else:
                loops.append(sweep_forever(store, rail, clock, settings))
            await asyncio.gather(*loops)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
