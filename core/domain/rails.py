"""Rails and Rail tokens: which rail a Mandate's instrument is on (see GLOSSARY.md)."""

import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

RailName = Literal["razorpay", "mock"]

# The currencies each rail can charge. Razorpay recurring payments are INR only; the mock
# rail takes whatever the Mandate is in (None).
RAIL_CURRENCIES: dict[RailName, frozenset[str] | None] = {
    "razorpay": frozenset({"INR"}),
    "mock": None,
}

# The failure code of a charge whose Pre-debit notice the rail marked failed: the rail
# refuses any debit after it, so the Payment fails instead of waiting for one (ADR-0017).
RAIL_NOTICE_FAILED = "RAIL_NOTICE_FAILED"

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

    def charges_in(self, currency: str) -> bool:
        allowed = RAIL_CURRENCIES[self.rail]
        return allowed is None or currency in allowed

    @model_validator(mode="after")
    def _no_card_number(self) -> Self:
        for value in (self.customer_ref, self.token_ref, self.display):
            if _PAN_LIKE.search(value):
                raise ValueError("a Rail token must not contain a card number")
        return self
