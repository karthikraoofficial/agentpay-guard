"""The same behaviour from the in-memory store and the Postgres store."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from adapters.memory.unit_of_work import InMemoryUnitOfWork
from adapters.postgres.store import PostgresStore
from core.domain.parties import ALL_SCOPES, ApiKeyGrant, Payer, PayerId, TenantId

AT = datetime(2026, 11, 2, tzinfo=UTC)


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> Iterator[InMemoryUnitOfWork | PostgresStore]:
    if request.param == "memory":
        yield InMemoryUnitOfWork()
    else:
        yield PostgresStore(create_engine(request.getfixturevalue("migrated_database_url")))


def _tenant(store: InMemoryUnitOfWork | PostgresStore, name: str) -> TenantId:
    tenant_id = TenantId(f"ten_contract_{name}_{id(store)}")
    store.create_tenant(tenant_id, name, AT)
    assert store.add_api_key(tenant_id, "apg_test_x", f"sha256:{tenant_id}", {"decide"}, AT)
    return tenant_id


def _payer(tenant_id: TenantId, payer_id: str) -> Payer:
    return Payer(
        payer_id=PayerId(payer_id),
        tenant_id=tenant_id,
        display_name="A",
        contact_ref="c",
        created_at=AT,
    )


def test_work_in_a_transaction_that_raises_is_not_stored(
    store: InMemoryUnitOfWork | PostgresStore,
) -> None:
    tenant = _tenant(store, "rollback")

    with pytest.raises(RuntimeError), store.transaction(tenant) as tx:
        tx.add_payer(_payer(tenant, "pay_rolled_back"))
        raise RuntimeError("fail after the write")

    with store.transaction(tenant) as tx:
        assert tx.get_payer(PayerId("pay_rolled_back")) is None


def test_work_in_a_clean_transaction_is_stored(store: InMemoryUnitOfWork | PostgresStore) -> None:
    tenant = _tenant(store, "commit")

    with store.transaction(tenant) as tx:
        tx.add_payer(_payer(tenant, "pay_kept"))

    with store.transaction(tenant) as tx:
        assert tx.get_payer(PayerId("pay_kept")) == _payer(tenant, "pay_kept")


def test_a_transaction_refuses_a_record_for_another_tenant(
    store: InMemoryUnitOfWork | PostgresStore,
) -> None:
    mine, theirs = _tenant(store, "mine"), _tenant(store, "theirs")

    with pytest.raises(ValueError, match="another Tenant"), store.transaction(mine) as tx:
        tx.add_payer(_payer(theirs, "pay_wrong"))


def test_a_key_hash_finds_its_tenant_and_scopes_and_nothing_else(
    store: InMemoryUnitOfWork | PostgresStore,
) -> None:
    tenant = _tenant(store, "keys")
    store.add_api_key(tenant, "apg_test_y", f"sha256:{tenant}_all", ALL_SCOPES, AT)

    assert store.api_key(f"sha256:{tenant}") == ApiKeyGrant(
        tenant_id=tenant, scopes=frozenset({"decide"})
    )
    assert store.api_key(f"sha256:{tenant}_all") == ApiKeyGrant(tenant_id=tenant, scopes=ALL_SCOPES)
    assert store.api_key("sha256:unknown") is None


def test_a_key_for_an_unknown_tenant_is_refused(store: InMemoryUnitOfWork | PostgresStore) -> None:
    assert (
        store.add_api_key(TenantId("ten_nope"), "apg_test_z", "sha256:nope", ALL_SCOPES, AT)
        is False
    )
    assert store.api_key("sha256:nope") is None
