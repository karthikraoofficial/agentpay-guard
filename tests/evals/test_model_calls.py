"""Per-call latency and timeouts for the live evals, on a scripted gateway (no network)."""

import pytest

from adapters.memory.model_gateway import ScriptedGateway
from core.ports import ModelTimeout, ModelUnavailable, TenantId
from evals.model_calls import TimedGateway, p95


def call(gateway: TimedGateway, timeout_ms: int = 2500) -> None:
    gateway.complete(
        tenant_id=TenantId("t"),
        purpose="intent_check",
        model="claude-haiku-5-5",
        prompt_version="p@1",
        system="s",
        user="u",
        max_tokens=8,
        timeout_ms=timeout_ms,
    )


def test_p95_is_the_nearest_rank_95th_percentile() -> None:
    assert p95([5]) == 5
    assert p95(list(range(1, 21))) == 19  # ceil(0.95 * 20) = the 19th
    assert p95(list(range(1, 101))) == 95
    assert p95([30, 10, 20]) == 30  # ceil(0.95 * 3) = the 3rd, so unsorted input is sorted


def test_a_reply_records_its_latency_and_a_timeout_records_the_timeout() -> None:
    gateway = TimedGateway(ScriptedGateway("MATCH", ModelTimeout("slow")))

    call(gateway)
    with pytest.raises(ModelTimeout):
        call(gateway, timeout_ms=2500)

    assert gateway.latencies_ms == [7, 2500]
    assert gateway.timeouts == 1
    assert gateway.p95_latency_ms == 2500


def test_a_call_that_failed_some_other_way_has_no_latency() -> None:
    """An API error or the spend cap says nothing about how fast the model answers."""
    gateway = TimedGateway(ScriptedGateway(ModelUnavailable("400")))

    with pytest.raises(ModelUnavailable):
        call(gateway)

    assert (gateway.latencies_ms, gateway.timeouts, gateway.p95_latency_ms) == ([], 0, None)


def test_timeouts_count_towards_p95() -> None:
    """19 quick replies and one timeout: the 19th of 20 is still a quick reply. A second
    timeout makes it the 19th."""
    one = TimedGateway(ScriptedGateway(*["MATCH"] * 19, ModelTimeout("slow")))
    two = TimedGateway(ScriptedGateway(*["MATCH"] * 18, ModelTimeout("a"), ModelTimeout("b")))
    for gateway in (one, two):
        for _ in range(20):
            try:
                call(gateway)
            except ModelTimeout:
                pass

    assert (one.p95_latency_ms, one.timeouts) == (7, 1)
    assert (two.p95_latency_ms, two.timeouts) == (2500, 2)
