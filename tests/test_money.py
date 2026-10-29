import pytest
from pydantic import ValidationError

from core.domain.money import Money


def test_rejects_float_amounts() -> None:
    with pytest.raises(ValidationError):
        Money(minor=125.5, currency="INR")  # type: ignore[arg-type]


def test_rejects_bad_currency_code() -> None:
    with pytest.raises(ValidationError):
        Money(minor=100, currency="inr")


def test_adds_same_currency() -> None:
    assert Money(minor=100, currency="INR") + Money(minor=250, currency="INR") == Money(
        minor=350, currency="INR"
    )


def test_refuses_to_add_mixed_currencies() -> None:
    with pytest.raises(ValueError, match="Cannot add"):
        Money(minor=100, currency="INR") + Money(minor=100, currency="USD")
