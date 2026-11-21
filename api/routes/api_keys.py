"""Key introspection: any key can read its own non-secret details (#244)."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header
from pydantic import BaseModel

from api.deps import StoreDep, Tenant, bearer_key_hash
from api.errors import ApiError
from core.domain.parties import ApiKeyScope

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


class ApiKeySelfOut(BaseModel):
    tenant_id: str
    key_prefix: str
    scopes: list[ApiKeyScope]
    created_at: datetime


@router.get("/self", response_model=ApiKeySelfOut)
def get_own_key(
    tenant: Tenant,
    store: StoreDep,
    authorization: Annotated[str | None, Header()] = None,
) -> ApiKeySelfOut:
    """The key that signed this request: its prefix, scopes and when it was made. Never
    the secret or its hash. Works with a key of any scope (ADR-0011)."""
    details = store.api_key_details(tenant, bearer_key_hash(authorization))
    if details is None:  # revoked between authentication and this read
        raise ApiError(401, "UNAUTHENTICATED", "Send a valid API key as `Authorization: Bearer`.")
    return ApiKeySelfOut(
        tenant_id=details.tenant_id,
        key_prefix=details.key_prefix,
        scopes=sorted(details.scopes),
        created_at=details.created_at,
    )
