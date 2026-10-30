"""Payments and Rail tokens: money moving for an allowed Decision (see GLOSSARY.md)."""

from typing import Annotated, Literal, NewType

from pydantic import BaseModel, ConfigDict, StringConstraints

PaymentId = NewType("PaymentId", str)

RailName = Literal["razorpay", "mock"]

_Ref = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[\w\-]+$")]
_Display = Annotated[str, StringConstraints(min_length=1, max_length=40)]


class RailToken(BaseModel):
    """The rail's reference to the Payer's instrument: refs and a masked display value only,
    never a card number or UPI PIN (non-negotiable 7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rail: RailName
    customer_ref: _Ref
    token_ref: _Ref
    display: _Display
