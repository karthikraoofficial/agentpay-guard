"""A Tenant's webhook endpoint: registering it, reading it and rotating its signing secret
(manage scope). The secret is returned once, by the request that makes it, and is stored
only encrypted (ADR-0015): an idempotent replay returns `secret: null`."""

from datetime import datetime

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict

from api.deps import ClockDep, IdemKey, ManageTenant, SecretBoxDep, StoreDep
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from core.domain.decisions import decision_time
from core.domain.webhooks import WebhookEndpoint, is_https_url, new_signing_secret
from core.ports import SecretBox, TenantId, TenantTransaction

router = APIRouter(prefix="/v1/webhook-endpoint", tags=["webhooks"])


class EndpointBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str


class EndpointOut(BaseModel):
    url: str
    created_at: AwareDatetime
    secret_rotated_at: AwareDatetime
    # Only in the response that made the secret: copy it now, it is never shown again.
    secret: str | None = None


def _out(endpoint: WebhookEndpoint, secret: str | None = None) -> EndpointOut:
    return EndpointOut(
        url=endpoint.url,
        created_at=endpoint.created_at,
        secret_rotated_at=endpoint.secret_rotated_at,
        secret=secret,
    )


def _without_secret(model: BaseModel) -> BaseModel:
    return model.model_copy(update={"secret": None})


@router.post("", response_model=EndpointOut)
def register_endpoint(
    request: Request,
    body: EndpointBody,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    box: SecretBoxDep,
    key: IdemKey,
) -> Response:
    """Register the Tenant's HTTPS endpoint (`201`, with the new signing secret), or move
    an existing one to a new URL (`200`, keeping its secret)."""

    def write(read_at: datetime) -> tuple[int, EndpointOut]:
        now = decision_time(read_at)
        current = tx.get_webhook_endpoint()
        if not is_https_url(body.url):
            raise ApiError(422, "WEBHOOK_URL_INVALID", "The endpoint must be an https:// URL.")
        if current is not None:
            moved = current.model_copy(update={"url": body.url})
            tx.save_webhook_endpoint(moved)
            return 200, _out(moved)
        secret = new_signing_secret()
        made = _endpoint(tenant, body.url, _encrypted(box, secret), now)
        tx.save_webhook_endpoint(made)
        return 201, _out(made, secret)

    with store.transaction(tenant) as tx:
        return run_idempotent(
            tx, key, request_hash(request, body), clock, write, kept_as=_without_secret
        )


@router.get("", response_model=EndpointOut)
def read_endpoint(tenant: ManageTenant, store: StoreDep) -> EndpointOut:
    """The Tenant's endpoint, never with its secret."""
    with store.transaction(tenant) as tx:
        return _out(_registered(tx))


@router.post("/rotate-secret", response_model=EndpointOut)
def rotate_secret(
    request: Request,
    tenant: ManageTenant,
    store: StoreDep,
    clock: ClockDep,
    box: SecretBoxDep,
    key: IdemKey,
) -> Response:
    """Replace the signing secret. Events sent from now on are signed with the new one,
    which this response shows once."""

    def write(read_at: datetime) -> tuple[int, EndpointOut]:
        now = decision_time(read_at)
        current = _registered(tx)
        secret = new_signing_secret()
        rotated = current.model_copy(
            update={"secret_ciphertext": _encrypted(box, secret), "secret_rotated_at": now}
        )
        tx.save_webhook_endpoint(rotated)
        return 200, _out(rotated, secret)

    with store.transaction(tenant) as tx:
        return run_idempotent(
            tx, key, request_hash(request, None), clock, write, kept_as=_without_secret
        )


def _endpoint(tenant: TenantId, url: str, ciphertext: str, at: datetime) -> WebhookEndpoint:
    return WebhookEndpoint(
        tenant_id=tenant, url=url, secret_ciphertext=ciphertext, created_at=at, secret_rotated_at=at
    )


def _encrypted(box: SecretBox, secret: str) -> str:
    try:
        return box.encrypt(secret)
    except RuntimeError:
        raise ApiError(
            503,
            "WEBHOOKS_NOT_CONFIGURED",
            "Tenant webhooks are off: the server has no AGENTPAY_WEBHOOK_ENCRYPTION_KEY.",
        ) from None


def _registered(tx: TenantTransaction) -> WebhookEndpoint:
    endpoint = tx.get_webhook_endpoint()
    if endpoint is None:
        raise ApiError(404, "WEBHOOK_ENDPOINT_NOT_FOUND", "No webhook endpoint is registered.")
    return endpoint
