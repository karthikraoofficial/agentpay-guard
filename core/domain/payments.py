"""Payments and Rail tokens: money moving for an allowed Decision (see GLOSSARY.md)."""

import re
from typing import Annotated, Literal, NewType, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

PaymentId = NewType("PaymentId", str)

RailName = Literal["razorpay", "mock"]

_Ref = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[\w\-]+$")]
_Display = Annotated[str, StringConstraints(min_length=1, max_length=40)]

# 13 to 19 digits, optionally split by spaces or dashes: the shape of a card number (PAN).
_PAN_LIKE = re.compile(r"\d(?:[ -]?\d){12,18}")


class RailToken(BaseModel):
    """The rail's reference to the Payer's instrument: refs and a masked display value only,
    never a card number or UPI PIN (non-negotiable 7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rail: RailName
    customer_ref: _Ref
    token_ref: _Ref
    display: _Display

    @model_validator(mode="after")
    def _no_card_number(self) -> Self:
        for value in (self.customer_ref, self.token_ref, self.display):
            if _PAN_LIKE.search(value):
                raise ValueError("a Rail token must not contain a card number")
        return self
