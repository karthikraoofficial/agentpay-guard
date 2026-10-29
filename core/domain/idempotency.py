"""Idempotency: a repeated write with the same key gets the first response back."""

from datetime import datetime, timedelta
from typing import NewType

from pydantic import BaseModel, ConfigDict

IdempotencyKey = NewType("IdempotencyKey", str)

# How long a key's stored response is replayed. After this, the key counts as new.
RETENTION = timedelta(hours=24)


class IdempotencyRecord(BaseModel):
    """The response a write produced, stored with the hash of the request that made it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: IdempotencyKey
    request_hash: str
    status_code: int
    body: bytes
    created_at: datetime

    def expired(self, now: datetime) -> bool:
        return now - self.created_at >= RETENTION
