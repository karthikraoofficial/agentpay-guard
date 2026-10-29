"""Money as integer minor units plus an ISO 4217 currency (CLAUDE.md non-negotiable 8)."""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class Money(BaseModel):
    """An amount in minor units (paise, cents). Floats are rejected, not rounded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    minor: StrictInt = Field(ge=0, description="Amount in minor units, e.g. 12_500 = INR 125.00")
    currency: CurrencyCode

    def __add__(self, other: Self) -> Self:
        if other.currency != self.currency:
            raise ValueError(f"Cannot add {other.currency} to {self.currency}")
        return self.model_copy(update={"minor": self.minor + other.minor})
