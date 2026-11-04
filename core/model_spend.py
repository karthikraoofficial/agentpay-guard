"""The Model spend cap (#134, ADR-0020): a daily limit on model calls per Tenant and
purpose, so a leaked key on a public demo can't run up a bill.

CappedGateway wraps any ModelGateway. Before each call it counts the call against the
Tenant's limit for that purpose and UTC day. Over the limit it doesn't call the model: it
traces the call with outcome `capped` and raises ModelSpendCapReached, a ModelUnavailable,
so the caller fails closed as for any outage. A purpose with no limit is never called.

The model never sits on the decision path (non-negotiable 1), so Decisions and their
replay are unaffected; only what the Intent check or narrative writer gets back changes.
"""

from collections.abc import Mapping
from datetime import UTC

from core.ports import (
    AttributeValue,
    Clock,
    Completion,
    Effort,
    ModelCallCounter,
    ModelGateway,
    ModelSpendCapReached,
    Telemetry,
    TenantId,
)
from core.settings import Settings


class CappedGateway:
    def __init__(
        self,
        inner: ModelGateway,
        counter: ModelCallCounter,
        clock: Clock,
        telemetry: Telemetry,
        limits: Mapping[str, int],
    ) -> None:
        self._inner = inner
        self._counter = counter
        self._clock = clock
        self._telemetry = telemetry
        self._limits = dict(limits)

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
        limit = self._limits.get(purpose, 0)
        day = self._clock.now().astimezone(UTC).date()
        if not self._counter.take_model_call(tenant_id, purpose, day, limit):
            attributes: dict[str, AttributeValue] = {
                "tenant_id": tenant_id,
                "purpose": purpose,
                "model": model,
                "prompt_version": prompt_version,
                "outcome": "capped",
                "daily_cap": limit,
            }
            with self._telemetry.start_span("model_gateway.complete", attributes):
                pass
            raise ModelSpendCapReached(
                f"the Model spend cap of {limit} {purpose} call(s) a day is used up"
            )
        return self._inner.complete(
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


def daily_caps(settings: Settings) -> dict[str, int]:
    """The limit per purpose, from settings."""
    return {
        "intent_check": settings.intent_check_daily_cap,
        "dispute_narrative": settings.dispute_narrative_daily_cap,
    }


_conforms: type[ModelGateway] = CappedGateway  # mypy checks it matches the port
