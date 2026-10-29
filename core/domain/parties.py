"""Tenants, Payers and Agents: who is on each side of a Mandate (see GLOSSARY.md)."""

import hashlib
import re
import secrets
from datetime import datetime
from typing import Annotated, Literal, NewType

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

TenantId = NewType("TenantId", str)
PayerId = NewType("PayerId", str)
AgentId = NewType("AgentId", str)

AgentStatus = Literal["active", "disabled"]

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE = re.compile(r"\+?[\d\s().-]{7,}")


def _not_raw_contact(value: str) -> str:
    """A contact_ref is a token the Tenant can resolve, never the contact itself
    (minimising personal data under the DPDP Act)."""
    if _EMAIL.fullmatch(value) or _PHONE.fullmatch(value):
        raise ValueError("contact_ref must be a reference or token, not an email or phone")
    return value


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ContactRef = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    AfterValidator(_not_raw_contact),
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Payer(_Frozen):
    payer_id: PayerId
    tenant_id: TenantId
    display_name: Name
    contact_ref: ContactRef
    created_at: datetime


class Agent(_Frozen):
    agent_id: AgentId
    tenant_id: TenantId
    name: Name
    status: AgentStatus
    created_at: datetime


def new_id(prefix: str) -> str:
    """A random, URL-safe identifier such as `pay_3fJ…`."""
    return f"{prefix}_{secrets.token_urlsafe(16)}"


def new_api_key(*, live: bool) -> str:
    """A Tenant API key. Shown once at creation; only its hash is stored."""
    return f"apg_{'live' if live else 'test'}_{secrets.token_urlsafe(32)}"


def hash_api_key(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def api_key_prefix(key: str) -> str:
    """The non-secret start of a key, kept so a Tenant can tell its keys apart."""
    return key[:16]
