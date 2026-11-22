"""Every Scenario file in the catalogue, run through the step engine against the in-process
app, ends with its expected result (#273). This is the one place a journey is proven.

Each time a run waits, the test plays the worker and the people, as the demo CLI tests do.
"""

import pytest

from demo.__main__ import main, run_scenario
from demo.engine import Run, play
from demo.scenario import Scenario, catalogue
from tests.api.conftest import Api
from tests.conftest import make_settings
from tests.demo.conftest import clients
from tests.demo.operator import Operator

pytestmark = pytest.mark.anyio

SCENARIOS = list(catalogue().values())


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
async def test_each_scenario_ends_with_its_expected_result(
    api: Api, keys: tuple[str, str], scenario: Scenario
) -> None:
    operator = Operator(api, [])
    async with clients(api, keys) as (agent, tenant):
        run = Run(scenario, agent=agent, tenant=tenant, now=api.clock.now)

        cards = await play(run, wait=operator.wait, patience=20)

    assert cards[-1].finished
    assert cards[-1].mismatches == []


async def test_a1_shows_the_decision_and_its_audit_record(api: Api, keys: tuple[str, str]) -> None:
    operator = Operator(api, [])
    async with clients(api, keys) as (agent, tenant):
        run = Run(catalogue()["A1"], agent=agent, tenant=tenant, now=api.clock.now)

        cards = await play(run, wait=operator.wait, patience=20)

    decided = next(card for card in cards if card.call and card.call.name == "request_decision")
    assert decided.actor == "agent"
    assert decided.decision is not None
    assert decided.decision.outcome == "allow"
    assert decided.decision.reason_codes == []
    assert decided.decision.audit_record_id.startswith("aud")
    assert decided.decision.policy_pack.startswith("default@")
    assert [line.speaker for line in decided.chat] == ["asha", "shopbot"]


async def test_the_cli_prints_a_scenario_as_it_plays(api: Api, keys: tuple[str, str]) -> None:
    out: list[str] = []
    operator = Operator(api, [])
    async with clients(api, keys) as (agent, tenant):
        code = await run_scenario(
            catalogue()["A1"],
            agent=agent,
            tenant=tenant,
            say=out.append,
            wait=operator.wait,
            now=api.clock.now,
            patience=20,
        )

    text = "\n".join(out)
    assert code == 0
    assert "Scenario A1: A normal grocery top-up" in text
    assert "Asha: Reorder my usual milk and bread, please." in text
    assert "request_decision: allow" in text
    assert "As expected." in text
    assert keys[0] not in text and keys[1] not in text


def test_the_cli_refuses_an_unknown_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    keys = {"demo_agent_key": "apg_test_a", "demo_tenant_key": "apg_test_b"}

    code = main(["--base-url", "http://localhost:8000", "--scenario", "Z9"], make_settings(**keys))

    assert code == 2
    assert "No Scenario Z9" in capsys.readouterr().err
