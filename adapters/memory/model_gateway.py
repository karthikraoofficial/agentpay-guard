"""ModelGateway without a real provider: a scripted test double, and the fallback used
when no model API key is configured. No network, no cost."""

from collections import deque
from dataclasses import dataclass

from core.ports import Completion, ModelGatewayError, ModelUnavailable, TenantId


@dataclass(frozen=True)
class GatewayCall:
    tenant_id: str
    purpose: str
    model: str
    prompt_version: str
    system: str
    user: str
    max_tokens: int
    timeout_ms: int


class ScriptedGateway:
    """Replies with each scripted text (or raises each scripted error) in turn, and
    records every call. With an empty script it answers `default_reply`."""

    def __init__(self, *script: str | ModelGatewayError, default_reply: str = "MATCH") -> None:
        self.script: deque[str | ModelGatewayError] = deque(script)
        self.default_reply = default_reply
        self.stop_reason = "end_turn"
        self.calls: list[GatewayCall] = []

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
    ) -> Completion:
        self.calls.append(
            GatewayCall(
                tenant_id, purpose, model, prompt_version, system, user, max_tokens, timeout_ms
            )
        )
        reply = self.script.popleft() if self.script else self.default_reply
        if isinstance(reply, ModelGatewayError):
            raise reply
        return Completion(
            text=reply,
            stop_reason=self.stop_reason,
            model=model,
            prompt_version=prompt_version,
            tokens_in=120,
            tokens_out=2,
            cost_usd_micros=13,
            latency_ms=7,
        )


class UnconfiguredGateway:
    """Used when no model API key is configured: every call fails, so callers fail
    closed instead of the app refusing to start."""

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
    ) -> Completion:
        raise ModelUnavailable("no model API key is configured")
