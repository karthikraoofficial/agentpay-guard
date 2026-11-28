"""The demo CLI (#138): demo journey 1 (journeys 2, 3 and 4 are Scenarios E1, G1 and H1,
tested in tests/demo/test_scenarios.py) over HTTP against the in-process app, on the mock
rail with mock settle on. The CLI only talks HTTP."""

from typing import Any

import pytest

from demo.__main__ import main
from demo.journeys import Demo, StepFailed
from tests.api.conftest import NOW, Api
from tests.conftest import make_settings
from tests.demo.operator import demo_keys


@pytest.fixture
def keys(api: Api) -> tuple[str, str]:
    return demo_keys(api)


def demo(api: Api, keys: tuple[str, str], out: list[str]) -> Demo:
    agent_key, tenant_key = keys
    return Demo(
        client=api.client,
        agent_key=agent_key,
        tenant_key=tenant_key,
        say=out.append,
        now=api.clock.now,
    )


def test_journey_1_an_allow_becomes_a_payment(api: Api, keys: tuple[str, str]) -> None:
    out: list[str] = []

    payment = demo(api, keys, out).journey_1()

    assert payment["status"] == "scheduled"
    assert any("allow" in line for line in out)


def test_a_journey_never_prints_a_key(api: Api, keys: tuple[str, str]) -> None:
    out: list[str] = []
    run = demo(api, keys, out)

    run.journey_1()

    text = "\n".join(out)
    assert keys[0] not in text and keys[1] not in text


def test_a_failure_names_the_step(api: Api, keys: tuple[str, str]) -> None:
    out: list[str] = []
    wrong = ("apg_test_wrong", keys[1])

    with pytest.raises(StepFailed) as failed:
        demo(api, wrong, out).journey_1()

    assert failed.value.step == "Ask for a Decision"
    assert "401" in str(failed.value)


def test_the_cli_refuses_to_start_without_the_demo_keys(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--base-url", "http://localhost:8000"], settings=make_settings())

    assert code == 2
    assert "AGENTPAY_DEMO_AGENT_KEY" in capsys.readouterr().err


def test_dates_follow_the_server_clock(api: Api, keys: tuple[str, str]) -> None:
    out: list[str] = []
    run = demo(api, keys, out)
    run.journey_1()

    mandate: dict[str, Any] = api.client.get(
        f"/v1/mandates/{run.mandate_id}", headers={"Authorization": f"Bearer {keys[1]}"}
    ).json()

    assert mandate["valid_from"] < NOW.isoformat().replace("+00:00", "Z") < mandate["expires_at"]
