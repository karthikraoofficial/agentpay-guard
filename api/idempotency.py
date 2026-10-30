"""Run a write so that repeating it with the same Idempotency-Key is safe.

Inside the write's own transaction: claim the key (409 IDEMPOTENCY_IN_PROGRESS if
another call holds it), replay a stored response for the same request, refuse a
different request (409 IDEMPOTENCY_KEY_REUSED), or run the write and store its exact
response bytes. The record commits with the write, so a write that fails stores
nothing and a retry runs fresh.
"""

import hashlib
from collections.abc import Callable
from datetime import datetime

from fastapi import Request, Response
from pydantic import BaseModel

from api.errors import ApiError
from core.domain.canonical_json import canonical_json
from core.domain.idempotency import IdempotencyKey, IdempotencyRecord
from core.ports import Clock, TenantTransaction

REPLAYED = "Idempotent-Replayed"


def request_hash(request: Request, body: BaseModel | None) -> str:
    """Identifies the request a key was first used for: method, path and body."""
    fingerprint = {
        "method": request.method,
        "path": request.url.path,
        "body": None if body is None else body.model_dump(mode="json"),
    }
    return "sha256:" + hashlib.sha256(canonical_json(fingerprint)).hexdigest()


def run_idempotent(
    tx: TenantTransaction,
    key: IdempotencyKey,
    fingerprint: str,
    clock: Clock,
    write: Callable[[datetime], tuple[int, BaseModel]],
    *,
    kept_as: Callable[[BaseModel], BaseModel] | None = None,
) -> Response:
    """`write` gets the request time and returns (status code, response model).

    `kept_as` turns the response into what is kept for replays, for a response that holds
    a secret shown only once (a Consent link token): a replay then returns the redacted
    copy, and the secret is never at rest."""
    if not tx.claim_idempotency_key(key):
        raise ApiError(
            409,
            "IDEMPOTENCY_IN_PROGRESS",
            "A request with this Idempotency-Key is still being processed.",
            headers={"Retry-After": "1"},
        )
    now = clock.now()
    stored = tx.get_idempotency_record(key)
    if stored is not None and not stored.expired(now):
        if stored.request_hash != fingerprint:
            raise ApiError(
                409,
                "IDEMPOTENCY_KEY_REUSED",
                "This Idempotency-Key was already used for a different request.",
            )
        return _response(stored.status_code, stored.body, {REPLAYED: "true"})

    status_code, model = write(now)
    body = model.model_dump_json().encode("utf-8")
    kept = body if kept_as is None else kept_as(model).model_dump_json().encode("utf-8")
    tx.save_idempotency_record(
        IdempotencyRecord(
            key=key, request_hash=fingerprint, status_code=status_code, body=kept, created_at=now
        )
    )
    return _response(status_code, body, {})


def _response(status_code: int, body: bytes, headers: dict[str, str]) -> Response:
    return Response(
        content=body, status_code=status_code, media_type="application/json", headers=headers
    )
