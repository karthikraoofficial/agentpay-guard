import pytest

from adapters.memory.unit_of_work import InMemoryUnitOfWork
from core.ports import TenantId, UnitOfWork


def test_a_transaction_is_scoped_to_the_tenant_that_opened_it() -> None:
    uow: UnitOfWork = InMemoryUnitOfWork()

    with uow.transaction(TenantId("ten_a")) as tx:
        assert tx.tenant_id == TenantId("ten_a")


def test_an_error_inside_a_transaction_propagates() -> None:
    uow: UnitOfWork = InMemoryUnitOfWork()

    with pytest.raises(RuntimeError, match="boom"), uow.transaction(TenantId("ten_a")):
        raise RuntimeError("boom")
