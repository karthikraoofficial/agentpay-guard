"""A circuit breaker on the API's rail calls (#226, ADR-0025).

Decisions never call the rail. Payments and refunds do, and during a rail outage each call
waits for its timeout and leaves a Payment `scheduled` or a Refund `pending`. After
`failures` unknown outcomes in a row the breaker opens: for `open_for`, a payment or refund
is refused at once with how long to wait, before anything is written. After that, one
request is let through as a trial. Its answer closes the breaker; another unknown outcome
opens it again. A trial that never reaches the rail (refused for another reason) frees the
next request after another `open_for`.

Any answer from the rail, `failed` included, is an answer: only unknown outcomes
(`RailUnavailable`) count. The state lives in one API process, which is enough for one
API on one VM (ADR-0021). It reports whether it is open as a gauge, set when it opens or
closes (#228).
"""

import math
import threading
from datetime import datetime, timedelta

from core.ops_metrics import report_breaker
from core.ports import Telemetry


class RailBreaker:
    def __init__(self, failures: int, open_for: timedelta, telemetry: Telemetry) -> None:
        if failures < 1:
            raise ValueError("the breaker opens after at least one failure")
        self._telemetry = telemetry
        report_breaker(telemetry, is_open=False)
        self._failures = failures
        self._open_for = open_for
        self._lock = threading.Lock()  # the API calls the rail from several threads
        self._in_a_row = 0
        self._open_until: datetime | None = None
        self._trial_since: datetime | None = None

    def refusal(self, now: datetime) -> int | None:
        """Seconds to wait if a rail call must not be made at `now`, or None if it may.
        Letting the first call after the open period through makes it the trial."""
        with self._lock:
            if self._open_until is None:
                return None
            if now < self._open_until:
                return _seconds(self._open_until - now)
            if self._trial_since is not None and now < self._trial_since + self._open_for:
                return _seconds(self._trial_since + self._open_for - now)
            self._trial_since = now
            return None

    def answered(self) -> bool:
        """The rail answered (captured, scheduled, failed or processed): it is up. Returns
        whether this closed the breaker."""
        with self._lock:
            was_open = self._open_until is not None
            self._in_a_row = 0
            self._open_until = None
            self._trial_since = None
        if was_open:
            report_breaker(self._telemetry, is_open=False)
        return was_open

    def trip(self, now: datetime) -> None:
        """Open now, as `failures` unknown outcomes in a row would: a simulated rail outage
        (ADR-0032) shouldn't need a visitor to fail five payments first."""
        with self._lock:
            was_closed = self._open_until is None
            self._in_a_row = max(self._in_a_row, self._failures)
            self._open_until = now + self._open_for
            self._trial_since = None
        if was_closed:
            report_breaker(self._telemetry, is_open=True)

    def unavailable(self, now: datetime) -> bool:
        """The rail call's outcome is unknown. Returns whether this opened the breaker."""
        with self._lock:
            # Only an answer resets the run, so a failed trial is past the threshold already.
            self._in_a_row += 1
            if self._in_a_row < self._failures:
                return False
            was_closed = self._open_until is None
            self._open_until = now + self._open_for
            self._trial_since = None
        if was_closed:
            report_breaker(self._telemetry, is_open=True)
        return was_closed


def _seconds(wait: timedelta) -> int:
    return max(1, math.ceil(wait.total_seconds()))
