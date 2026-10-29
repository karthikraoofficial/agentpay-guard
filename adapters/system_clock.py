from datetime import UTC, datetime

from core.ports import Clock


class SystemClock:
    """The real Clock: the machine's current time in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


_conforms: type[Clock] = SystemClock  # mypy checks the adapter matches the port
