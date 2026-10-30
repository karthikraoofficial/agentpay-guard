"""Postgres-only guarantees: atomic Decision + Audit writes and an append-only table."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from adapters.postgres.store import PostgresStore, PostgresTransaction
from api.main import create_app
from core.domain.audit import SealedAuditRecord
from core.ports import TenantId
from tests.api.conftest import NOW, Api, MovableClock
from tests.api.test_decisions import Pay
from tests.api.test_tenants_payers_agents import assert_problem
from tests.conftest import make_settings


class _AuditWriteFails(PostgresTransaction):
    def append_audit_record(self, sealed: SealedAuditRecord) -> None:
        raise RuntimeError("audit insert failed")


class _FailingStore(PostgresStore):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
        self.engine = engine

    @contextmanager
    def transaction(self, tenant_id: TenantId) -> Iterator[PostgresTransaction]:
        with self.engine.begin() as conn:
            yield _AuditWriteFails(conn, tenant_id)


@pytest.fixture
def engine(migrated_database_url: str) -> Engine:
    return create_engine(migrated_database_url)


@pytest.fixture
def pg(engine: Engine) -> Pay:
    store = PostgresStore(engine)
    clock = MovableClock(NOW)
    return Pay(Api(TestClient(create_app(make_settings(), store=store, clock=clock)), store, clock))


def _count(engine: Engine, sql: str, **params: Any) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(sql), params).scalar_one())


def test_if_the_audit_write_fails_no_decision_is_stored(pg: Pay, engine: Engine) -> None:
    tenant_id = pg.tenant_id()
    failing = create_app(make_settings(), store=_FailingStore(engine), clock=MovableClock(NOW))
    client = TestClient(failing, raise_server_exceptions=False)
    body = {
        "mandate_id": pg.mandate_id,
        "agent_id": pg.agent_ids[0],
        "amount": {"minor": 100_00, "currency": "INR"},
        "merchant": {"id": "m", "name": "M", "mcc": "5411"},
    }

    response = client.post(
        "/v1/decisions", headers={**pg.tenant, "Idempotency-Key": "k-fail"}, json=body
    )

    assert_problem(response, 500, "INTERNAL")
    assert _count(engine, "SELECT count(*) FROM decisions WHERE tenant_id = :t", t=tenant_id) == 0
    assert (
        _count(engine, "SELECT count(*) FROM audit_records WHERE tenant_id = :t", t=tenant_id) == 0
    )
    failed_key = "SELECT count(*) FROM idempotency_keys WHERE tenant_id = :t AND key = 'k-fail'"
    assert _count(engine, failed_key, t=tenant_id) == 0


def test_if_a_step_up_rolls_back_no_outbox_event_is_left(pg: Pay, engine: Engine) -> None:
    tenant_id = pg.tenant_id()
    failing = create_app(make_settings(), store=_FailingStore(engine), clock=MovableClock(NOW))
    client = TestClient(failing, raise_server_exceptions=False)
    body = {
        "mandate_id": pg.mandate_id,
        "agent_id": pg.agent_ids[0],
        "amount": {"minor": 6_000_00, "currency": "INR"},  # above the step-up threshold
        "merchant": {"id": "m", "name": "M", "mcc": "5411"},
    }

    response = client.post(
        "/v1/decisions", headers={**pg.tenant, "Idempotency-Key": "k-fail-2"}, json=body
    )

    assert response.status_code == 500
    assert (
        _count(engine, "SELECT count(*) FROM outbox_events WHERE tenant_id = :t", t=tenant_id) == 0
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_records SET kind = 'void' WHERE tenant_id = :t",
        "DELETE FROM audit_records WHERE tenant_id = :t",
        "TRUNCATE audit_records",
    ],
    ids=["update", "delete", "truncate"],
)
def test_audit_records_cannot_be_changed_or_removed(
    pg: Pay, engine: Engine, statement: str
) -> None:
    pg.ask()

    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text(statement), {"t": pg.tenant_id()})

    assert (
        _count(engine, "SELECT count(*) FROM audit_records WHERE tenant_id = :t", t=pg.tenant_id())
        == 1
    )
