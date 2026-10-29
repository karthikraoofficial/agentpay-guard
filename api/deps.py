"""Request dependencies: the Tenant, store, clock, telemetry and idempotency key."""

from typing import Annotated, Protocol

from fastapi import Depends, Header, Request

from api.errors import ApiError
from core.domain.parties import hash_api_key
from core.policy.packs import LoadedPack
from core.ports import Clock, IdempotencyKey, Telemetry, TenantDirectory, TenantId, UnitOfWork


class Store(UnitOfWork, TenantDirectory, Protocol):
    """What the API needs from storage: tenant-scoped transactions and key lookup."""


def get_store(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


def get_pack(request: Request) -> LoadedPack:
    pack: LoadedPack = request.app.state.policy_pack
    return pack


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


def get_telemetry(request: Request) -> Telemetry:
    telemetry: Telemetry = request.app.state.telemetry
    return telemetry


def authenticated_tenant(
    store: Annotated[Store, Depends(get_store)],
    authorization: Annotated[str | None, Header()] = None,
) -> TenantId:
    """The Tenant whose API key signed the request. tenant_id never comes from a body."""
    scheme, _, key = (authorization or "").partition(" ")
    tenant_id = store.tenant_for_key(hash_api_key(key)) if scheme == "Bearer" and key else None
    if tenant_id is None:
        raise ApiError(401, "UNAUTHENTICATED", "Send a valid API key as `Authorization: Bearer`.")
    return tenant_id


def idempotency_key(
    _tenant: Annotated[TenantId, Depends(authenticated_tenant)],
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> IdempotencyKey:
    """Every write needs an Idempotency-Key (non-negotiable 4). Checked after auth."""
    if not key or len(key) > 255:
        raise ApiError(
            400, "IDEMPOTENCY_KEY_MISSING", "Send an Idempotency-Key header (1-255 chars)."
        )
    return IdempotencyKey(key)


Tenant = Annotated[TenantId, Depends(authenticated_tenant)]
StoreDep = Annotated[Store, Depends(get_store)]
ClockDep = Annotated[Clock, Depends(get_clock)]
PackDep = Annotated[LoadedPack, Depends(get_pack)]
TelemetryDep = Annotated[Telemetry, Depends(get_telemetry)]
IdemKey = Annotated[IdempotencyKey, Depends(idempotency_key)]
