"""Signals: facts about an Agent payment request beyond the Mandate's terms (GLOSSARY.md).

The API layer works them out before deciding and passes them to `decide()` as a value,
which the Audit record keeps, so replay never recomputes a Signal (ADR-0003). A Signal
can at most send a request to step_up (ADR-0006). Each Signal adds a field here with a
default, so records written before it existed still parse and replay.
"""

from pydantic import BaseModel, ConfigDict, Field


class Signals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Decisions on the Mandate, of any Outcome, in the pack's Velocity window before
    # this one. None means it wasn't worked out, which the velocity rule treats as high.
    velocity_count: int | None = Field(default=None, ge=0)
