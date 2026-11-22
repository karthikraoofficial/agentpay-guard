"""Latency and timeouts of the model calls a live eval makes.

`TimedGateway` wraps the ModelGateway an eval runs on and records every call: a reply at
the latency the gateway measured, a timeout at its `timeout_ms` (it took at least that
long, and the job got no answer). A call that failed any other way (an API error, the
Model spend cap) records nothing, since it says nothing about how fast the model is.
p95 is over every recorded call, timeouts included, so a model that often runs out of
time shows it there and in the timeout count.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from core.ports import Completion, Effort, ModelGateway, ModelTimeout, TenantId


def p95(samples: Sequence[float]) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


@dataclass
class TimedGateway:
    inner: ModelGateway
    latencies_ms: list[int] = field(default_factory=list)
    timeouts: int = 0

    @property
    def p95_latency_ms(self) -> int | None:
        return int(p95(self.latencies_ms)) if self.latencies_ms else None

    def complete(
        self,
        tenant_id: TenantId,
        purpose: str,
        model: str,
        prompt_version: str,
        system: str,
        user: str,
        max_tokens: int,
        timeout_ms: int,
        effort: Effort | None = None,
    ) -> Completion:
        try:
            completion = self.inner.complete(
                tenant_id=tenant_id,
                purpose=purpose,
                model=model,
                prompt_version=prompt_version,
                system=system,
                user=user,
                max_tokens=max_tokens,
                timeout_ms=timeout_ms,
                effort=effort,
            )
        except ModelTimeout:
            self.latencies_ms.append(timeout_ms)
            self.timeouts += 1
            raise
        self.latencies_ms.append(completion.latency_ms)
        return completion


def latency_line(p95_latency_ms: int | None, timed_calls: int, timeouts: int) -> str:
    p95_ms = "n/a" if p95_latency_ms is None else f"{p95_latency_ms} ms"
    return f"latency: p95 {p95_ms} over {timed_calls} call(s), {timeouts} timed out"
