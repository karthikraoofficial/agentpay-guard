"""The Claude adapter against the real API. Costs a fraction of a cent per run.

Run with `uv run pytest -m contract -o addopts=""`. Needs AGENTPAY_ANTHROPIC_API_KEY;
skipped without it. Never part of the default run or CI.
"""

import pytest

from adapters.claude.gateway import ClaudeGateway
from adapters.memory.telemetry import InMemoryTelemetry
from core.ports import ModelTimeout, TenantId
from core.settings import get_settings

pytestmark = pytest.mark.contract

MODEL = "claude-haiku-5-5"


@pytest.fixture
def gateway() -> ClaudeGateway:
    key = get_settings().anthropic_api_key
    if key is None:
        pytest.skip("AGENTPAY_ANTHROPIC_API_KEY is not set")
    return ClaudeGateway(key, InMemoryTelemetry())


def _ask(gateway: ClaudeGateway, timeout_ms: int):  # type: ignore[no-untyped-def]
    return gateway.complete(
        tenant_id=TenantId("ten_contract"),
        purpose="contract_test",
        model=MODEL,
        prompt_version="contract@1",
        system="Reply with exactly one word: OK.",
        user="Ping.",
        max_tokens=64,
        timeout_ms=timeout_ms,
    )


def test_one_real_call_returns_text_usage_and_cost(gateway: ClaudeGateway) -> None:
    completion = _ask(gateway, timeout_ms=30_000)

    assert completion.text.strip()
    assert completion.model == MODEL
    assert completion.tokens_in > 0 and completion.tokens_out > 0
    assert completion.cost_usd_micros >= 1


def test_a_tiny_timeout_surfaces_as_model_timeout(gateway: ClaudeGateway) -> None:
    with pytest.raises(ModelTimeout):
        _ask(gateway, timeout_ms=1)
