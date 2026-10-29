"""One byte-exact JSON form for everything that is hashed or compared.

Policy pack hashes, audit record hashes, idempotency body hashes and replay all use
this serialiser, so equal values always give equal bytes (ADR-0002, ADR-0004).
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime

from pydantic import BaseModel

JsonValue = None | bool | int | str | list["JsonValue"] | dict[str, "JsonValue"]


def canonical_json(value: object) -> bytes:
    """Serialise with sorted keys, no whitespace and UTF-8 without ASCII escaping.

    Datetimes become RFC 3339 UTC strings truncated to milliseconds with a `Z`, and
    Pydantic models (such as Money) become their fields. Raises TypeError for floats
    (money is integer minor units) and for any other type without one canonical form.
    """
    return json.dumps(
        _normalise(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _normalise(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        raise TypeError(f"float {value!r} has no canonical form; use integer minor units")
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, BaseModel):
        return _normalise(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {_key(k): _normalise(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_normalise(v) for v in value]
    raise TypeError(f"{type(value).__name__} has no canonical JSON form")


def _key(key: object) -> str:
    if not isinstance(key, str):
        raise TypeError(f"mapping key {key!r} must be a str")
    return key


def _timestamp(at: datetime) -> str:
    if at.tzinfo is None or at.utcoffset() is None:
        raise TypeError(f"datetime {at!r} has no timezone; pass an aware UTC datetime")
    utc = at.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
