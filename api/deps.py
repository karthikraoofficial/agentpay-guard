"""Request dependencies: the Tenant, store, clock, telemetry and idempotency key."""

from collections.abc import Callable
from typing import Annotated, Protocol

from fastapi import Depends, Header, Request

from api.errors import ApiError
from core.domain.parties import ApiKeyGrant, ApiKeyScope, hash_api_key
from core.policy.packs import LoadedPack
from core.ports import (
    Clock,
    EventBus,
    IdempotencyKey,
    ModelGateway,
    Telemetry,
    TenantDirectory,
    TenantId,
    UnitOfWork,
    Workflow,
)
from core.settings import Settings


class Store(UnitOfWork, TenantDirectory, EventBus, Protocol):
    """What the API needs from storage: tenant-scoped transactions, key lookup, and the
    outbox the relay reads."""


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


def get_gateway(request: Request) -> ModelGateway:
    gateway: ModelGateway = request.app.state.gateway
    return gateway


def get_workflow(request: Request) -> Workflow:
    workflow: Workflow = request.app.state.workflow
    return workflow


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def authenticated_key(
    store: Annotated[Store, Depends(get_store)],
    authorization: Annotated[str | None, Header()] = None,
) -> ApiKeyGrant:
    """The key that signed the request. tenant_id never comes from a body."""
    scheme, _, key = (authorization or "").partition(" ")
    grant = store.api_key(hash_api_key(key)) if scheme == "Bearer" and key else None
    if grant is None:
        raise ApiError(401, "UNAUTHENTICATED", "Send a valid API key as `Authorization: Bearer`.")
    return grant


def authenticated_tenant(grant: Annotated[ApiKeyGrant, Depends(authenticated_key)]) -> TenantId:
    """The Tenant of a key with any scope: for reads, and revoking a Mandate."""
    return grant.tenant_id


def _scoped(scope: ApiKeyScope) -> Callable[[ApiKeyGrant], TenantId]:
    def tenant_with_scope(grant: Annotated[ApiKeyGrant, Depends(authenticated_key)]) -> TenantId:
        if scope not in grant.scopes:
            raise ApiError(
                403, "INSUFFICIENT_SCOPE", f"This API key doesn't have the `{scope}` scope."
            )
        return grant.tenant_id

    return tenant_with_scope


def require_idempotency_key(key: str | None) -> IdempotencyKey:
    """Every write needs an Idempotency-Key (non-negotiable 4)."""
    if not key or len(key) > 255:
        raise ApiError(
            400, "IDEMPOTENCY_KEY_MISSING", "Send an Idempotency-Key header (1-255 chars)."
        )
    return IdempotencyKey(key)


def idempotency_key(
    _tenant: Annotated[TenantId, Depends(authenticated_tenant)],
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> IdempotencyKey:
    """Checked after auth, so an unauthenticated write is a 401, not a 400."""
    return require_idempotency_key(key)


Tenant = Annotated[TenantId, Depends(authenticated_tenant)]
# Scoped Tenants (ADR-0011): Agent traffic, consent resolution, and Tenant-side writes.
DecideTenant = Annotated[TenantId, Depends(_scoped("decide"))]
ConsentTenant = Annotated[TenantId, Depends(_scoped("consent"))]
ManageTenant = Annotated[TenantId, Depends(_scoped("manage"))]
StoreDep = Annotated[Store, Depends(get_store)]
ClockDep = Annotated[Clock, Depends(get_clock)]
PackDep = Annotated[LoadedPack, Depends(get_pack)]
TelemetryDep = Annotated[Telemetry, Depends(get_telemetry)]
GatewayDep = Annotated[ModelGateway, Depends(get_gateway)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
WorkflowDep = Annotated[Workflow, Depends(get_workflow)]
IdemKey = Annotated[IdempotencyKey, Depends(idempotency_key)]
