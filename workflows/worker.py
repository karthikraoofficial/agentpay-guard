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
from collections.abc import Mapping
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
from core.demo_switches import SwitchedRail, switchable_writer
from core.domain.rails import RailName
from core.evidence import NarrativeWriter
from core.model_spend import CappedGateway, daily_caps
from core.ops_metrics import BacklogStore, report_backlog_once, report_sweep
from core.ports import (
    Clock,
    EventBus,
    ModelCallCounter,
    PaymentRail,
    Telemetry,
    TenantId,
    UnitOfWork,
)
from core.reconcile import SweepResult, SweepStore, sweep
from core.relay import relay_once
from core.settings import Settings, get_settings
from core.tenant_webhooks import TenantWebhooks
from workflows.activities import ConsentActivities, DisputeActivities
from workflows.client import TemporalWorkflow
from workflows.consent import StepUpConsent
from workflows.dispute import DisputePack

log = logging.getLogger(__name__)


# How often the backlog metrics are read: under the collector's 60 s export, so each
# export carries a fresh reading.
BACKLOG_INTERVAL_SECONDS = 30

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
    telemetry: Telemetry | None = None,
) -> Worker:
    activities = ConsentActivities(store, clock)
    disputes = DisputeActivities(store, clock, packs_dir, writer, telemetry)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[StepUpConsent, DisputePack],
        activities=[activities.notify, activities.expire, disputes.build, disputes.expire],
        activity_executor=pool,
    )


def narrative_writer(
    settings: Settings, counter: ModelCallCounter, clock: Clock, telemetry: Telemetry
) -> NarrativeWriter | None:
    """The dispute evidence agent, or None to have every narrative written by code. Its
    model calls count against each Tenant's Model spend cap (ADR-0020)."""
    if not settings.dispute_narrative_enabled:
        return None
    gateway = CappedGateway(
        build_gateway(settings, telemetry), counter, clock, telemetry, daily_caps(settings)
    )
    return DisputeNarrativeWriter(
        gateway, settings.dispute_narrative_model, settings.dispute_narrative_timeout_ms
    )


def demo_writer(
    writer: NarrativeWriter | None, settings: Settings, store: UnitOfWork
) -> NarrativeWriter | None:
    """The writer, honouring the AI-off Demo control for the Demo Tenant (ADR-0032)."""
    if writer is None or settings.demo_controls_tenant_id is None:
        return writer
    return switchable_writer(writer, store, TenantId(settings.demo_controls_tenant_id))


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


async def backlog_forever(
    store: BacklogStore, telemetry: Telemetry, clock: Clock, interval_seconds: float
) -> None:
    """Until cancelled, each interval: report the outbox's waiting workflow events and the
    Consent requests stuck past expiry (#228). Synchronous reads, so each in a thread."""
    while True:
        await asyncio.to_thread(report_backlog_once, store, telemetry, clock.now())
        await asyncio.sleep(interval_seconds)


def sweep_rails(settings: Settings, store: UnitOfWork) -> dict[RailName, PaymentRail]:
    """Each rail the debit step and the sweep ask, by name (ADR-0033). The mock rail is
    left out without mock settle: MockRail keeps its charges inside the API process, so a
    worker's own MockRail would know none of them and the sweep would wrongly fail every
    mock Payment. With mock settle on (ADR-0019), it asks a rail that captures from the
    stored Payment instead."""
    rails: dict[RailName, PaymentRail] = {}
    for name in sorted(settings.reachable_rails):
        if name != "mock":
            rails[name] = build_rail(name, settings)
        elif settings.mock_settle_after_seconds is not None:
            settling: PaymentRail = SettlingMockRail(store)
            if settings.demo_controls_tenant_id is not None:
                # Demo control switches reach the debit step and the sweep too (ADR-0032).
                settling = SwitchedRail(
                    settling,
                    store,
                    TenantId(settings.demo_controls_tenant_id),
                    SystemClock(),
                    timedelta(seconds=settings.demo_rail_outage_seconds),
                )
            rails[name] = settling
    return rails


def sweep_age(settings: Settings, rail: RailName) -> timedelta:
    """How old a scheduled Payment on `rail` is before the sweep asks about it."""
    if rail == "mock" and settings.mock_settle_after_seconds is not None:
        return timedelta(seconds=settings.mock_settle_after_seconds)
    return timedelta(seconds=settings.reconcile_after_seconds)


async def sweep_forever(
    store: SweepStore,
    rails: Mapping[RailName, PaymentRail],
    clock: Clock,
    settings: Settings,
    telemetry: Telemetry,
) -> None:
    """Until cancelled, each interval and for each rail: debit the Payments whose Pre-debit
    notice period has ended, then reconcile the scheduled ones; then report the sweep's
    metrics, added up over the rails. Every pass is synchronous, so each runs in a thread."""
    demo = settings.demo_controls_tenant_id
    demo_tenant = None if demo is None else TenantId(demo)  # Demo controls, ADR-0032
    while True:
        result = SweepResult(checked=0, errors=0)
        for name, rail in rails.items():
            debits = await asyncio.to_thread(run_debit_step, store, rail, clock, demo_tenant, name)
            if debits.checked:
                log.info(
                    "%s debit step checked %d Payment(s), %d error(s)",
                    name,
                    debits.checked,
                    debits.errors,
                )
            older_than = sweep_age(settings, name)
            result += await asyncio.to_thread(
                sweep, store, rail, clock, older_than, name, demo_tenant
            )
        report_sweep(telemetry, result)
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
    telemetry = OtelTelemetry.from_settings(settings)
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    with ThreadPoolExecutor(max_workers=8) as pool:
        worker = consent_worker(
            client,
            settings.temporal_task_queue,
            store,
            clock,
            pool,
            settings.policy_packs_dir,
            demo_writer(narrative_writer(settings, store, clock, telemetry), settings, store),
            telemetry,
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
                ),
                backlog_forever(store, telemetry, clock, BACKLOG_INTERVAL_SECONDS),
            ]
            rails = sweep_rails(settings, store)
            if "mock" in settings.reachable_rails and "mock" not in rails:
                log.info("mock rail not swept: mock Payments stay scheduled (no mock settle)")
            if rails:
                loops.append(sweep_forever(store, rails, clock, settings, telemetry))
            try:
                await asyncio.gather(*loops)
            finally:
                telemetry.shutdown()  # flush the last metrics


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
