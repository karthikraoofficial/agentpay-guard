from datetime import UTC, datetime
from pathlib import Path

from core.domain.money import Money
from core.ports import Clock


def decide(clock: Clock) -> Money:
    _ = Path('x'), datetime.now(UTC)
    return Money(minor=0, currency='INR')
