"""The Claude adapter for ModelGateway: the only module that imports the Anthropic SDK.

Each call enforces its own `timeout_ms` with SDK retries off, so a caller's latency
budget is the whole budget. Cost comes from the price table below in integer
micro-USD. Every call emits a `model_gateway.complete` span carrying usage and outcome,
never the prompt or reply text (non-negotiable 10).
"""

import time
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx2
from pydantic import SecretStr

from core.ports import (
    AttributeValue,
    Completion,
    Effort,
    ModelTimeout,
    ModelUnavailable,
    Telemetry,
    TenantId,
)


@dataclass(frozen=True)
class Price:
    """Micro-USD per million tokens. Above `long_prompt_from` input tokens the whole
    request is billed at the long-prompt rates, where the model has them."""

    input: int
    output: int
    long_prompt_from: int | None = None
    long_input: int = 0
    long_output: int = 0


# First-party API rates (USD per MTok x 1,000,000). Update when Anthropic's prices change.
PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(100_000, 500_000, 100_000, 500_000, 2_500_000),
    "claude-sonnet-5-5": Price(2_000_000, 10_000_000),
    "claude-opus-5-5": Price(4_000_000, 20_000_000),
}


def cost_usd_micros(model: str, tokens_in: int, tokens_out: int) -> int:
    """Cost of one call, rounded up to a whole micro-USD."""
    price = PRICES[model]
    rate_in, rate_out = price.input, price.output
    if price.long_prompt_from is not None and tokens_in > price.long_prompt_from:
        rate_in, rate_out = price.long_input, price.long_output
    total = tokens_in * rate_in + tokens_out * rate_out
    return -(-total // 1_000_000)


class ClaudeGateway:
    def __init__(
        self,
        api_key: SecretStr,
        telemetry: Telemetry,
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        http_client = (
            anthropic.DefaultHttpxClient(transport=transport) if transport is not None else None
        )
        self._client = anthropic.Anthropic(
            api_key=api_key.get_secret_value(), max_retries=0, http_client=http_client
        )
        self._telemetry = telemetry

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
        if model not in PRICES:
            raise ValueError(f"no price for model {model!r}; add it to PRICES")
        attributes: dict[str, AttributeValue] = {
            "tenant_id": tenant_id,
            "purpose": purpose,
            "model": model,
            "prompt_version": prompt_version,
            "timeout_ms": timeout_ms,
        }
        if effort is not None:
            attributes["effort"] = effort
        extra: dict[str, Any] = {} if effort is None else {"output_config": {"effort": effort}}
        with self._telemetry.start_span("model_gateway.complete", attributes) as span:
            started = time.monotonic()
            try:
                message = self._client.with_options(timeout=timeout_ms / 1000).messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    **extra,
                )
            except anthropic.APITimeoutError as exc:
                span.set_attribute("outcome", "timeout")
                span.set_attribute("latency_ms", _elapsed_ms(started))
                raise ModelTimeout(f"{model} gave no reply within {timeout_ms} ms") from exc
            except anthropic.APIStatusError as exc:
                span.set_attribute("outcome", "error")
                span.set_attribute("http_status", exc.status_code)
                span.set_attribute("latency_ms", _elapsed_ms(started))
                raise ModelUnavailable(f"{model} returned HTTP {exc.status_code}") from exc
            except anthropic.APIConnectionError as exc:
                span.set_attribute("outcome", "error")
                span.set_attribute("latency_ms", _elapsed_ms(started))
                raise ModelUnavailable(f"could not reach the API for {model}") from exc

            latency_ms = _elapsed_ms(started)
            tokens_in = message.usage.input_tokens
            tokens_out = message.usage.output_tokens
            cost = cost_usd_micros(model, tokens_in, tokens_out)
            stop_reason = str(message.stop_reason)
            for key, value in (
                ("outcome", "ok"),
                ("stop_reason", stop_reason),
                ("tokens_in", tokens_in),
                ("tokens_out", tokens_out),
                ("cost_usd_micros", cost),
                ("latency_ms", latency_ms),
            ):
                span.set_attribute(key, value)
            text = "".join(b.text for b in message.content if b.type == "text")
            return Completion(
                text=text,
                stop_reason=stop_reason,
                model=model,
                prompt_version=prompt_version,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd_micros=cost,
                latency_ms=latency_ms,
            )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
