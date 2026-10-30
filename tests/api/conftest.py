"""API tests run once against the in-memory store and once against Postgres, so the
fake can't drift from the real adapter. When each test ends, every Decision it made is
replayed byte-identical and every Audit chain it touched is verified."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from adapters.memory.clock import FixedClock
from adapters.memory.model_gateway import ScriptedGateway
from adapters.memory.telemetry import InMemoryTelemetry
from adapters.memory.unit_of_work import InMemoryUnitOfWork
from adapters.memory.workflow import InMemoryWorkflow
from adapters.postgres.store import PostgresStore
from api.admin import create_key, create_tenant, replay_record, verify_chain
from api.main import create_app
from core.domain.parties import ALL_SCOPES, ApiKeyScope, hash_api_key
from core.ports import TenantId
from core.settings import Settings
from tests.conftest import make_settings

PACKS = Path(__file__).resolve().parents[2] / "policy_packs"

NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
_keys = count()


class MovableClock:
    """A Clock tests can move forward."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def advance(self, by: timedelta) -> None:
        self.at += by


@dataclass
class Api:
    client: TestClient
    store: InMemoryUnitOfWork | PostgresStore
    clock: MovableClock
    tenants: list[TenantId] = field(default_factory=list)
    telemetry: InMemoryTelemetry = field(default_factory=InMemoryTelemetry)
    gateway: ScriptedGateway = field(default_factory=ScriptedGateway)
    workflow: InMemoryWorkflow = field(default_factory=InMemoryWorkflow)
    packs: Path = PACKS

    def use_packs(self, packs: Path) -> None:
        """Serve from `packs` (e.g. one made by tests.packs.release_test_pack), with its
        active pack deciding, and replay against it when the test ends."""
        app = create_app(
            make_settings(policy_packs_dir=packs),
            store=self.store,
            clock=self.clock,
            telemetry=self.telemetry,
            gateway=self.gateway,
            workflow=self.workflow,
        )
        self.client = TestClient(app)
        self.packs = packs

    def use_settings(self, settings: Settings) -> None:
        """Serve with `settings` (same store, clock, fakes and packs)."""
        app = create_app(
            settings.model_copy(update={"policy_packs_dir": self.packs}),
            store=self.store,
            clock=self.clock,
            telemetry=self.telemetry,
            gateway=self.gateway,
            workflow=self.workflow,
        )
        self.client = TestClient(app)

    def client_with(self, clock: object, *, raise_server_exceptions: bool = True) -> TestClient:
        """A client for an app sharing this store but reading time from `clock`."""
        app = create_app(
            make_settings(),
            store=self.store,
            clock=clock,  # type: ignore[arg-type]
            telemetry=self.telemetry,
            workflow=self.workflow,
        )
        return TestClient(app, raise_server_exceptions=raise_server_exceptions)

    def new_tenant(self, name: str = "acme") -> dict[str, str]:
        """Create a Tenant and return headers for a key with every scope, like a key from
        before scopes existed. Scope tests make narrower keys with `new_key`."""
        tenant_id, _, _ = create_tenant(self.store, name, live=False, clock=FixedClock(NOW))
        self.tenants.append(tenant_id)
        return self.new_key(tenant_id, ALL_SCOPES)

    def new_key(
        self, tenant_id: TenantId, scopes: set[ApiKeyScope] | frozenset[ApiKeyScope]
    ) -> dict[str, str]:
        key = create_key(
            self.store, tenant_id, frozenset(scopes), live=False, clock=FixedClock(NOW)
        )
        assert key is not None
        return {"Authorization": f"Bearer {key}"}

    def tenant_of(self, headers: dict[str, str]) -> TenantId:
        grant = self.store.api_key(hash_api_key(headers["Authorization"].removeprefix("Bearer ")))
        assert grant is not None
        return grant.tenant_id

    def assert_evidence_holds(self) -> None:
        """Every chain verifies and every recorded Decision replays byte-identical."""
        messages: list[str] = []
        for tenant_id in self.tenants:
            assert verify_chain(self.store, tenant_id, messages.append) == 0, messages
            with self.store.transaction(tenant_id) as tx:
                rows, _ = tx.read_audit_chain()
            for row in rows:
                code = replay_record(
                    self.store, tenant_id, row.audit_record_id, self.packs, messages.append
                )
                assert code == 0, messages

    def post(self, path: str, headers: dict[str, str], json: object | None = None):  # type: ignore[no-untyped-def]
        """POST with a fresh Idempotency-Key, as a well-behaved client would."""
        return self.client.post(
            path, headers={**headers, "Idempotency-Key": f"key-{next(_keys)}"}, json=json
        )


@pytest.fixture(params=["memory", "postgres"])
def api(request: pytest.FixtureRequest) -> Iterator[Api]:
    store: InMemoryUnitOfWork | PostgresStore
    if request.param == "memory":
        store = InMemoryUnitOfWork()
    else:
        url = request.getfixturevalue("migrated_database_url")
        store = PostgresStore(create_engine(url))
    clock = MovableClock(NOW)
    telemetry = InMemoryTelemetry()
    gateway = ScriptedGateway()
    workflow = InMemoryWorkflow()
    app = create_app(
        make_settings(),
        store=store,
        clock=clock,
        telemetry=telemetry,
        gateway=gateway,
        workflow=workflow,
    )
    api = Api(
        TestClient(app), store, clock, telemetry=telemetry, gateway=gateway, workflow=workflow
    )
    yield api
    api.assert_evidence_holds()
