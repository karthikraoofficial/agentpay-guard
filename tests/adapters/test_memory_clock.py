from datetime import UTC, datetime

from adapters.memory.clock import FixedClock
from core.ports import Clock


def test_fixed_clock_returns_the_instant_it_was_set_to() -> None:
    at = datetime(2026, 11, 1, 9, 15, tzinfo=UTC)
    clock: Clock = FixedClock(at)

    assert clock.now() == at
    assert clock.now() == at
