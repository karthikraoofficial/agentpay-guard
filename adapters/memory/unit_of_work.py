from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from core.ports import TenantId, UnitOfWork


@dataclass(frozen=True)
class InMemoryTransaction:
    tenant_id: TenantId


class InMemoryUnitOfWork:
    """A UnitOfWork held in process memory, for tests that don't need Postgres."""

    @contextmanager
    def transaction(self, tenant_id: TenantId) -> Iterator[InMemoryTransaction]:
        yield InMemoryTransaction(tenant_id)


_conforms: type[UnitOfWork] = InMemoryUnitOfWork  # mypy checks the fake matches the port
