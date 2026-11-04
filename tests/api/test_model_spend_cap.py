"""The Model spend cap (#134): a daily limit on model calls per Tenant and purpose,
counted in the store and checked before each call. Over the limit the model isn't called
and callers fail closed: the Intent check is unknown, the narrative is written by code."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agents.dispute_narrative import NarrativeWriter
from core.domain.disputes import DisputeId
from core.evidence import build_evidence_pack
from core.model_spend import CappedGateway
from core.ports import ModelSpendCapReached, TenantId
from tests.api.conftest import NOW, Api
from tests.api.test_disputes import DISPUTE_RULES, MODEL, Disputes, _opened
from tests.api.test_intent_decisions import Flow, groceries, outcome
from tests.api.test_rail_webhooks import SECRET
from tests.conftest import make_settings
from tests.packs import release_test_pack, signals_pack

LIMITS = {"intent_check": 2, "dispute_narrative": 1}


def capped(api: Api, limits: dict[str, int] = LIMITS) -> CappedGateway:
    return CappedGateway(api.gateway, api.store, api.clock, api.telemetry, limits)


def call(gateway: CappedGateway, tenant_id: TenantId, purpose: str = "intent_check") -> Any:
    return gateway.complete(
        tenant_id=tenant_id,
        purpose=purpose,
        model="claude-haiku-5-5",
        prompt_version="test@1",
        system="system",
        user="user",
        max_tokens=5,
        timeout_ms=1000,
    )


def tenant(api: Api, name: str = "acme") -> TenantId:
    return api.tenant_of(api.new_tenant(name))


def test_calls_up_to_the_limit_reach_the_model_and_the_next_does_not(api: Api) -> None:
    gateway, acme = capped(api), tenant(api)

    call(gateway, acme)
    call(gateway, acme)
    with pytest.raises(ModelSpendCapReached):
        call(gateway, acme)

    assert len(api.gateway.calls) == 2


def test_each_tenant_and_each_purpose_has_its_own_count(api: Api) -> None:
    gateway, acme, globex = capped(api), tenant(api), tenant(api, "globex")
    call(gateway, acme)
    call(gateway, acme)

    call(gateway, globex)
    call(gateway, acme, "dispute_narrative")
    with pytest.raises(ModelSpendCapReached):
        call(gateway, acme, "dispute_narrative")

    assert len(api.gateway.calls) == 4


def test_the_count_starts_again_at_midnight_utc(api: Api) -> None:
    gateway, acme = capped(api, {"intent_check": 1}), tenant(api)
    call(gateway, acme)
    midnight = (NOW + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    api.clock.advance(midnight - NOW - timedelta(seconds=1))
    with pytest.raises(ModelSpendCapReached):
        call(gateway, acme)
    api.clock.advance(timedelta(seconds=1))
    call(gateway, acme)

    assert len(api.gateway.calls) == 2


def test_a_purpose_with_no_limit_is_never_called(api: Api) -> None:
    gateway = capped(api, {})

    with pytest.raises(ModelSpendCapReached):
        call(gateway, tenant(api))

    assert api.gateway.calls == []


def test_a_capped_call_is_traced_with_the_cap_as_its_outcome(api: Api) -> None:
    gateway, acme = capped(api, {"intent_check": 0}), tenant(api)

    with pytest.raises(ModelSpendCapReached):
        call(gateway, acme)

    [span] = [s for s in api.telemetry.spans if s.name == "model_gateway.complete"]
    assert span.attributes["outcome"] == "capped"
    assert span.attributes["daily_cap"] == 0
    assert (span.attributes["tenant_id"], span.attributes["purpose"]) == (acme, "intent_check")


# --- Callers fail closed ------------------------------------------------------------------


@pytest.fixture
def flow(api: Api, tmp_path: Path) -> Flow:
    api.use_packs(signals_pack(tmp_path))
    api.use_settings(make_settings(intent_check_daily_cap=1))
    return Flow(api)


def test_an_intent_check_over_the_cap_is_unknown_and_the_decision_steps_up(flow: Flow) -> None:
    assert outcome(flow.pay(intent_id=flow.intent())) == ("allow", [])

    response = flow.check(**groceries())
    intent_id = response.json()["intent_id"]

    stored = flow.read(intent_id).json()
    assert (stored["verdict"], stored["failure_cause"]) == ("unknown", "capped")
    assert outcome(flow.pay(intent_id=intent_id)) == ("step_up", ["INTENT_UNKNOWN"])
    assert len(flow.api.gateway.calls) == 1


@pytest.fixture
def disputes(api: Api, tmp_path: Path) -> Disputes:
    api.use_packs(release_test_pack(tmp_path, "1.0.1", {"disputes": DISPUTE_RULES}))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    return Disputes(api)


def test_a_narrative_over_the_cap_is_written_by_code(disputes: Disputes) -> None:
    dispute_id, _ = _opened(disputes)
    api = disputes.api
    writer = NarrativeWriter(capped(api, {"dispute_narrative": 0}), MODEL, 45_000)

    build_evidence_pack(
        api.store, disputes.tenant_id(), DisputeId(dispute_id), api.clock, api.packs, writer
    )

    pack = disputes.get(dispute_id).json()["pack"]
    assert (pack["narrative_source"], pack["narrative_note"]) == (
        "template",
        "model narrative unavailable",
    )
    assert [a["error"] for a in pack["narrative_attempts"]] == ["ModelSpendCapReached"]
    assert api.gateway.calls == []
