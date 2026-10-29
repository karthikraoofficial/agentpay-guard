"""The Claude adapter behind the ModelGateway port, against a fake HTTP transport (no
network). The real API is covered by tests/adapters/test_claude_gateway_contract.py."""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from adapters.claude.gateway import ClaudeGateway
from adapters.memory.telemetry import InMemoryTelemetry
from core.ports import ModelTimeout, ModelUnavailable, TenantId

PROMPT = "This prompt text must never reach telemetry."


def reply(
    text: str = "MATCH", tokens_in: int = 400, tokens_out: int = 3, **extra: Any
) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    } | extra


def gateway(
    handler: Callable[[httpx2.Request], httpx2.Response], telemetry: InMemoryTelemetry
) -> ClaudeGateway:
    return ClaudeGateway(
        SecretStr("sk-test-not-real"), telemetry, transport=httpx2.MockTransport(handler)
    )


def ask(gw: ClaudeGateway, model: str = "claude-haiku-5-5", timeout_ms: int = 800):  # type: ignore[no-untyped-def]
    return gw.complete(
        tenant_id=TenantId("ten_1"),
        purpose="intent_check",
        model=model,
        prompt_version="intent_check@1",
        system=PROMPT,
        user=PROMPT,
        max_tokens=16,
        timeout_ms=timeout_ms,
    )


def test_a_reply_comes_back_with_its_usage_and_integer_cost() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=reply())

    completion = ask(gateway(handler, InMemoryTelemetry()))

    assert completion.text == "MATCH"
    assert completion.stop_reason == "end_turn"
    assert (completion.model, completion.prompt_version) == ("claude-haiku-5-5", "intent_check@1")
    assert (completion.tokens_in, completion.tokens_out) == (400, 3)
    # Haiku 5.5: $0.10 / $0.50 per MTok = 400 * 0.1 + 3 * 0.5 = 41.5 micro-USD, rounded up.
    assert completion.cost_usd_micros == 42
    assert completion.latency_ms >= 0
    (body,) = sent
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 16
    assert "temperature" not in body  # rejected on current models and API versions


def test_every_call_emits_a_span_with_usage_and_no_prompt_or_reply_text() -> None:
    telemetry = InMemoryTelemetry()

    ask(gateway(lambda _: httpx2.Response(200, json=reply(text=PROMPT)), telemetry))

    (span,) = telemetry.spans
    assert span.name == "model_gateway.complete"
    assert {k: span.attributes[k] for k in ("tenant_id", "purpose", "model", "prompt_version")} == {
        "tenant_id": "ten_1",
        "purpose": "intent_check",
        "model": "claude-haiku-5-5",
        "prompt_version": "intent_check@1",
    }
    assert (span.attributes["tokens_in"], span.attributes["tokens_out"]) == (400, 3)
    assert span.attributes["cost_usd_micros"] == 42
    assert span.attributes["outcome"] == "ok"
    assert all(PROMPT not in str(v) for v in span.attributes.values())


def _counting(raise_or_respond: Callable[[httpx2.Request], httpx2.Response]):  # type: ignore[no-untyped-def]
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return raise_or_respond(request)

    return handler, calls


def _timeout(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("slow", request=request)


def _refused_connection(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("refused", request=request)


def test_a_timeout_surfaces_as_model_timeout_after_one_attempt() -> None:
    handler, calls = _counting(_timeout)
    telemetry = InMemoryTelemetry()

    with pytest.raises(ModelTimeout):
        ask(gateway(handler, telemetry))

    assert len(calls) == 1  # no SDK retries: the caller's budget is the whole budget
    assert telemetry.spans[0].attributes["outcome"] == "timeout"


@pytest.mark.parametrize("status", [429, 500, 529])
def test_provider_errors_surface_as_model_unavailable(status: int) -> None:
    body = {"type": "error", "error": {"type": "api_error", "message": "nope"}}
    handler, calls = _counting(lambda _: httpx2.Response(status, json=body))
    telemetry = InMemoryTelemetry()

    with pytest.raises(ModelUnavailable):
        ask(gateway(handler, telemetry))

    assert len(calls) == 1
    assert telemetry.spans[0].attributes["outcome"] == "error"
    assert telemetry.spans[0].attributes["http_status"] == status


def test_a_network_failure_surfaces_as_model_unavailable() -> None:
    with pytest.raises(ModelUnavailable):
        ask(gateway(_refused_connection, InMemoryTelemetry()))


def test_an_unknown_model_is_a_configuration_error_before_any_call() -> None:
    handler, calls = _counting(lambda _: httpx2.Response(200, json=reply()))

    with pytest.raises(ValueError, match="no price for model"):
        ask(gateway(handler, InMemoryTelemetry()), model="claude-nonexistent")

    assert calls == []


def test_a_refusal_comes_back_as_a_reply_with_its_stop_reason() -> None:
    refused = reply(text="", stop_reason="refusal")

    completion = ask(gateway(lambda _: httpx2.Response(200, json=refused), InMemoryTelemetry()))

    assert (completion.text, completion.stop_reason) == ("", "refusal")


def test_long_prompts_are_billed_at_the_long_prompt_rate() -> None:
    from adapters.claude.gateway import cost_usd_micros

    assert cost_usd_micros("claude-haiku-5-5", 100_000, 0) == 10_000  # $0.01
    assert cost_usd_micros("claude-haiku-5-5", 100_001, 0) == 50_001  # whole request at $0.50/MTok
    assert cost_usd_micros("claude-haiku-5-5", 1, 0) == 1  # rounds up, never down to free
