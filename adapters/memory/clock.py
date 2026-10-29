from datetime import datetime

from core.ports import Clock


class FixedClock:
    """A Clock that always returns the instant it was given."""

    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at


_conforms: type[Clock] = FixedClock  # mypy checks the fake matches the port
