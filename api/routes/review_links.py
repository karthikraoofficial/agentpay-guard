"""/v1/review-link: the hosted dispute viewer's API.

Public: the Review link token is the only credential, and whoever holds it is treated as
the reviewer the Tenant named when it minted the link. It arrives in the `Review-Link`
header, never in a path, so no access log records it, and only its hash is looked up. A
link works until a newer one is minted, Sign-off through it uses it up, it expires, or
the Dispute is closed; any other link is `REVIEW_LINK_INVALID`, whatever the reason, so a
guessed or stale token learns nothing. A Regenerate through the link keeps it working.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from api.deps import ClockDep, SettingsDep, StoreDep, require_idempotency_key
from api.errors import ApiError
from api.idempotency import request_hash, run_idempotent
from api.routes.disputes import DisputeOut, checked_regenerate, checked_sign_off, dispute_out
from core.domain.consent import hash_link_token
from core.domain.disputes import Dispute, Reviewer, ReviewLink
from core.ports import TenantTransaction

router = APIRouter(prefix="/v1/review-link", tags=["review link"])

LinkToken = Annotated[str | None, Header(alias="Review-Link")]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


class ReviewView(BaseModel):
    reviewer: Reviewer
    dispute: DisputeOut


class LinkSignOffBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The version the reviewer read; refused unless it is still the latest.
    pack_version: int = Field(ge=1)


def _invalid() -> ApiError:
    return ApiError(
        404, "REVIEW_LINK_INVALID", "This review link no longer works. Ask for a new one."
    )


def _link(store: StoreDep, token: str | None) -> ReviewLink:
    link = store.review_link(hash_link_token(token)) if token else None
    if link is None:
        raise _invalid()
    return link


def _working(tx: TenantTransaction, link: ReviewLink, now: datetime, *, lock: bool) -> Dispute:
    """Re-check inside the transaction: the link still works and its Dispute is open."""
    current = tx.get_review_link(link.token_hash)
    dispute = (tx.lock_dispute if lock else tx.get_dispute)(link.dispute_id)
    if current is None or dispute is None or not current.works(dispute.status, now):
        raise _invalid()
    return dispute


@router.get("")
def view_review_link(store: StoreDep, clock: ClockDep, token: LinkToken = None) -> ReviewView:
    """The Dispute and its pack, as the reviewer is to read them."""
    link = _link(store, token)
    with store.transaction(link.tenant_id) as tx:
        dispute = _working(tx, link, clock.now(), lock=False)
        return ReviewView(reviewer=link.reviewer, dispute=dispute_out(tx, dispute))


@router.post("/sign-off", response_model=DisputeOut)
def sign_off_via_link(
    request: Request,
    body: LinkSignOffBody,
    store: StoreDep,
    clock: ClockDep,
    token: LinkToken = None,
    key: IdempotencyHeader = None,
) -> Response:
    """The reviewer approves the pack version they read, as the reviewer the link names.
    This uses the link up."""

    def act(tx: TenantTransaction, link: ReviewLink, dispute: Dispute, at: datetime) -> DisputeOut:
        signed = checked_sign_off(tx, dispute, body.pack_version, link.reviewer, at)
        tx.use_review_link(link.token_hash, at)
        return signed

    return _through(request, body, store, clock, token, key, act)


@router.post("/regenerate", response_model=DisputeOut)
def regenerate_via_link(
    request: Request,
    store: StoreDep,
    clock: ClockDep,
    settings: SettingsDep,
    token: LinkToken = None,
    key: IdempotencyHeader = None,
) -> Response:
    """The reviewer asks for the next pack version. The link keeps working."""

    def act(tx: TenantTransaction, link: ReviewLink, dispute: Dispute, at: datetime) -> DisputeOut:
        return checked_regenerate(tx, dispute, settings.dispute_pack_versions_max, at)

    return _through(request, None, store, clock, token, key, act)


def _through(
    request: Request,
    body: BaseModel | None,
    store: StoreDep,
    clock: ClockDep,
    token: str | None,
    key: str | None,
    act: Callable[[TenantTransaction, ReviewLink, Dispute, datetime], DisputeOut],
) -> Response:
    link = _link(store, token)
    idempotency_key = require_idempotency_key(key)

    def write(read_at: datetime) -> tuple[int, DisputeOut]:
        dispute = _working(tx, link, read_at, lock=True)
        return 200, act(tx, link, dispute, read_at)

    with store.transaction(link.tenant_id) as tx:
        return run_idempotent(tx, idempotency_key, request_hash(request, body), clock, write)
