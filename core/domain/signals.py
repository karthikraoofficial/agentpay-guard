"""Signals: facts about an Agent payment request beyond the Mandate's terms (GLOSSARY.md).

The API layer works them out before deciding and passes them to `decide()` as a value,
which the Audit record keeps, so replay never recomputes a Signal (ADR-0003). A Signal
can at most send a request to step_up (ADR-0006). Each Signal adds a field here with a
default, so records written before it existed still parse and replay.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.domain.intents import FailureCause, IntentId, IntentVerdict
from core.domain.money import Money

# How the Intent check an Agent payment request cites was resolved. Only "used" gives
# the rules a verdict; every other value means no usable verdict.
IntentResolution = Literal[
    "used",
    "none_cited",
    "not_found",
    "wrong_mandate",
    "wrong_agent",
    "merchant_mismatch",
    "expired",
]


class IntentSignal(BaseModel):
    """What a Decision used from an Intent check. Never the Payer instruction or cart
    text: the content hash proves which content was checked (ADR-0007)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: IntentId
    verdict: IntentVerdict
    failure_cause: FailureCause | None
    quoted_total: Money
    model: str
    prompt_version: str
    content_hash: str


class Signals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Decisions on the Mandate, of any Outcome, in the pack's Velocity window before
    # this one. None means it wasn't worked out, which the velocity rule treats as high.
    velocity_count: int | None = Field(default=None, ge=0)
    # The Intent check the request cited, how it was resolved, and its details if used.
    # A missing resolution is treated like no usable verdict.
    cited_intent_id: IntentId | None = None
    intent_resolution: IntentResolution | None = None
    intent: IntentSignal | None = None
