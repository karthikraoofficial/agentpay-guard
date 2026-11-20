"""The worker's backlog metrics (#228): workflow events waiting in the outbox, by age, and
Consent requests past expiry with no resolution, the two symptoms of stuck consent
workflows (docs/runbooks/stuck-consent-workflows.md). Read across every Tenant, reported
as gauges with no Tenant or request ids."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from adapters.memory.telemetry import InMemoryTelemetry
from core.ops_metrics import report_backlog_once
from tests.api.conftest import NOW, Api
from tests.api.test_consent_decline import ATTESTATION, Declines
from tests.packs import phase_1_packs


@pytest.fixture
def declines(api: Api, tmp_path: Path) -> Declines:
    api.use_packs(phase_1_packs(tmp_path))
    return Declines(api)


def read(api: Api, at: datetime) -> dict[str, float]:
    """The gauges as the worker would report them at `at`. The reads span every Tenant, and
    the Postgres test database keeps other tests' rows, so tests compare against a
    baseline read at the same time before they add anything."""
    telemetry = InMemoryTelemetry()
    report_backlog_once(api.store, telemetry, at)
    found = {
        age: telemetry.last("agentpay.outbox.undelivered", age=age)
        for age in ("under_1m", "1m_to_10m", "over_10m")
    } | {"expired_unresolved": telemetry.last("agentpay.consent.expired_unresolved")}
    assert None not in found.values()  # every gauge is set, at zero too
    assert all(set(m.attributes) <= {"age"} for m in telemetry.metrics)  # no ids
    return {k: v for k, v in found.items() if v is not None}


def added(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    return {k: after[k] - before[k] for k in after}


def test_with_nothing_waiting_every_gauge_reads_zero() -> None:
    from adapters.memory.unit_of_work import InMemoryUnitOfWork

    telemetry = InMemoryTelemetry()
    report_backlog_once(InMemoryUnitOfWork(), telemetry, NOW)

    assert {m.value for m in telemetry.metrics} == {0}
    assert len(telemetry.metrics) == 4  # three outbox ages and the Consent count


def test_waiting_events_are_counted_by_age_and_late_expiries_by_resolution(
    declines: Declines,
) -> None:
    api = declines.api
    at = api.clock.now() + timedelta(minutes=16)
    before = read(api, at)
    stuck = declines.step_up()  # expires 10 minutes on
    declined = declines.step_up()
    declines.decline(declined["step_up"]["consent_request_id"], {"attestation": ATTESTATION})
    api.clock.advance(timedelta(minutes=10))
    declines.step_up()  # not expired yet
    api.clock.advance(timedelta(minutes=6))  # stuck is 6 minutes past its expiry
    assert api.clock.now() == at

    # No relay ran: three consent_requested events wait (16, 16 and 6 minutes old).
    assert added(before, read(api, at)) == {
        "under_1m": 0,
        "1m_to_10m": 1,
        "over_10m": 2,
        "expired_unresolved": 1,  # stuck only: declined is resolved, the third not yet due
    }
    assert stuck["outcome"] == "step_up"


def test_an_expiry_under_five_minutes_late_is_not_yet_stuck(declines: Declines) -> None:
    api = declines.api
    at = api.clock.now() + timedelta(minutes=14)
    before = read(api, at)
    declines.step_up()

    assert added(before, read(api, at))["expired_unresolved"] == 0


def test_the_worker_reports_the_backlog_on_its_interval(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workflows import worker

    waits: list[float] = []

    class Stop(Exception):
        pass

    async def stop(seconds: float) -> None:
        waits.append(seconds)
        raise Stop

    monkeypatch.setattr(worker.asyncio, "sleep", stop)
    telemetry = InMemoryTelemetry()

    with pytest.raises(Stop):
        asyncio.run(worker.backlog_forever(api.store, telemetry, api.clock, 30))

    assert telemetry.last("agentpay.consent.expired_unresolved") is not None
    assert waits == [30]
