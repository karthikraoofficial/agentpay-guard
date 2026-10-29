"""Signals over HTTP, on a test pack that enables them (#38)."""

import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from adapters.memory.telemetry import InMemoryTelemetry
from adapters.postgres.store import PostgresStore
from api.main import create_app
from tests.api.conftest import NOW, Api, MovableClock
from tests.api.test_decisions import Pay
from tests.conftest import make_settings
from tests.packs import VELOCITY, release_test_pack, rules_plus


def _velocity_pack(root: Path) -> Path:
    return release_test_pack(root, "1.0.1", {"velocity": VELOCITY, "rules": rules_plus("velocity")})


@pytest.fixture
def pay(api: Api, tmp_path: Path) -> Pay:
    api.use_packs(_velocity_pack(tmp_path))
    return Pay(api)


def small(**overrides: Any) -> dict[str, Any]:
    return {"amount": {"minor": 100_00, "currency": "INR"}} | overrides


def test_the_sixth_decision_in_ten_minutes_steps_up(pay: Pay) -> None:
    outcomes = []
    for _ in range(6):
        outcomes.append(pay.ask(**small()).json())
        pay.api.clock.advance(timedelta(minutes=1))

    assert [o["outcome"] for o in outcomes] == ["allow"] * 5 + ["step_up"]
    assert outcomes[-1]["reason_codes"] == ["VELOCITY_HIGH"]


def test_blocked_decisions_count_towards_velocity(pay: Pay) -> None:
    for _ in range(5):
        blocked = pay.ask(**small(merchant={"id": "m_9", "name": "Casino", "mcc": "7995"}))
        assert blocked.json()["outcome"] == "block"

    body = pay.ask(**small()).json()

    assert (body["outcome"], body["reason_codes"]) == ("step_up", ["VELOCITY_HIGH"])


def test_decisions_older_than_the_window_or_on_another_mandate_do_not_count(pay: Pay) -> None:
    for _ in range(5):
        pay.ask(**small())
    other = pay.create()["mandate_id"]

    assert pay.ask(mandate_id=other, **small()).json()["outcome"] == "allow"
    pay.api.clock.advance(timedelta(seconds=600))  # the five are now exactly 10 minutes old
    assert pay.ask(**small()).json()["outcome"] == "allow"


def test_the_audit_record_and_the_signals_span_carry_the_velocity_count(pay: Pay) -> None:
    for _ in range(3):
        pay.ask(**small())

    body = pay.ask(**small()).json()

    record = pay.audit(body["audit_record_id"]).record
    assert record.inputs.signals.velocity_count == 3
    spans = [s for s in pay.api.telemetry.spans if s.name == "signals.compute"]
    assert [s.attributes["velocity_count"] for s in spans] == [0, 1, 2, 3]


def test_parallel_requests_see_a_consistent_velocity_count(
    migrated_database_url: str, tmp_path: Path
) -> None:
    store = PostgresStore(create_engine(migrated_database_url, pool_size=6))
    clock = MovableClock(NOW)
    app = create_app(
        make_settings(policy_packs_dir=_velocity_pack(tmp_path)),
        store=store,
        clock=clock,
        telemetry=InMemoryTelemetry(),
    )
    pay = Pay(Api(TestClient(app), store, clock))
    for _ in range(4):
        pay.ask(**small())
    start = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def ask() -> None:
        start.wait()
        outcome = pay.ask(**small()).json()["outcome"]
        with lock:
            outcomes.append(outcome)

    workers = [threading.Thread(target=ask) for _ in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)

    assert sorted(outcomes) == ["allow", "step_up"]
