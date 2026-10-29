from datetime import datetime, timedelta, timezone

import pytest

from core.domain.canonical_json import canonical_json
from core.domain.money import Money


def test_equal_mappings_give_identical_bytes_whatever_the_key_order() -> None:
    a = {"b": 1, "a": {"y": [1, 2], "x": "₹"}}
    b = {"a": {"x": "₹", "y": [1, 2]}, "b": 1}

    assert canonical_json(a) == canonical_json(b)
    assert canonical_json(a) == '{"a":{"x":"₹","y":[1,2]},"b":1}'.encode()


@pytest.mark.parametrize(
    "value",
    [1.5, {"amount": 0.0}, [1, [2, {"x": 3.0}]], {"ok": 1, "nested": ({"f": 1e3},)}],
    ids=["top-level", "in-mapping", "deeply-nested", "in-tuple"],
)
def test_floats_are_rejected_anywhere(value: object) -> None:
    with pytest.raises(TypeError, match="float"):
        canonical_json(value)


def test_datetimes_serialise_as_utc_milliseconds_with_z() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    at = datetime(2026, 11, 1, 5, 30, 0, 123_999, tzinfo=ist)

    assert canonical_json({"at": at}) == b'{"at":"2026-11-01T00:00:00.123Z"}'


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(TypeError, match="timezone"):
        canonical_json(datetime(2026, 11, 1, 0, 0))


def test_money_serialises_as_currency_and_minor_units() -> None:
    assert canonical_json(Money(minor=500_000, currency="INR")) == (
        b'{"currency":"INR","minor":500000}'
    )
