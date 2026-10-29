"""API tests run once against the in-memory store and once against Postgres, so the
fake can't drift from the real adapter."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from adapters.memory.clock import FixedClock
from adapters.memory.unit_of_work import InMemoryUnitOfWork
from adapters.postgres.store import PostgresStore
from api.admin import create_tenant
from api.main import create_app
from tests.conftest import make_settings

NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
_keys = count()


@dataclass
class Api:
    client: TestClient
    store: InMemoryUnitOfWork | PostgresStore

    def new_tenant(self, name: str = "acme") -> dict[str, str]:
        """Create a Tenant and return headers that authenticate as it."""
        _, key = create_tenant(self.store, name, live=False, clock=FixedClock(NOW))
        return {"Authorization": f"Bearer {key}"}

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
    app = create_app(make_settings(), store=store, clock=FixedClock(NOW))
    yield Api(TestClient(app), store)
