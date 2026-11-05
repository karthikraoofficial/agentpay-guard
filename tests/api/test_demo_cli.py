"""The demo CLI (#138): the three demo journeys over HTTP against the in-process app, on
the mock rail with mock settle on.

The CLI only talks HTTP. Each time it waits, this test plays the worker (the mock settle
sweep, the outbox relay and the Evidence pack build) and the people the CLI waits for: the
Payer who opens the printed Consent link and approves, and the reviewer who opens the
printed Review link and signs off."""

import re
from datetime import timedelta
from typing import Any

import pytest

from adapters.memory.settling_rail import SettlingMockRail
from api.admin import create_tenant
from core.domain.disputes import DisputeId
from core.evidence import build_evidence_pack
from core.reconcile import sweep
from core.relay import relay_once
from demo.__main__ import main
from demo.journeys import Demo, StepFailed
from tests.api.conftest import NOW, Api, tenant_webhooks
from tests.conftest import make_settings

SETTLE_AFTER = timedelta(seconds=60)
LINK = re.compile(r"https?://\S+#(\S+)")


class Operator:
    """Everything outside the CLI: the worker, the Payer and the reviewer."""

    def __init__(self, api: Api, out: list[str]) -> None:
        self.api = api
        self.out = out
        self.used: set[str] = set()
        self.built: set[str] = set()
        self.waits = 0

    def _links(self, label: str) -> list[str]:
        tokens = []
        for line in self.out:
            match = LINK.search(line)
            if label in line and match and match.group(1) not in self.used:
                tokens.append(match.group(1))
        return tokens

    def wait(self) -> None:
        self.waits += 1
        api = self.api
        for token in self._links("Consent link"):
            self.used.add(token)
            approved = api.client.post(
                "/v1/consent-link/approve",
                headers={"Consent-Link": token, "Idempotency-Key": f"tap-{token[:8]}"},
            )
            assert approved.status_code == 200, approved.text
        for token in self._links("Review link"):
            self.used.add(token)
            signed = api.client.post(
                "/v1/review-link/sign-off",
                headers={"Review-Link": token, "Idempotency-Key": f"so-{token[:8]}"},
                json={"pack_version": 1},
            )
            assert signed.status_code == 200, signed.text
        # The worker: mock settle, the outbox relay, and each new Dispute's pack.
        api.clock.advance(timedelta(seconds=30))
        sweep(api.store, SettlingMockRail(api.store), api.clock, SETTLE_AFTER)
        relay_once(api.store, api.workflow, api.clock, tenant_webhooks(api.store, api.clock))
        for dispute_id, tenant_id in api.workflow.dispute_starts.items():
            if dispute_id not in self.built:
                self.built.add(dispute_id)
                assert (
                    build_evidence_pack(
                        api.store, tenant_id, DisputeId(dispute_id), api.clock, api.packs, None
                    )
                    == "built"
                )


@pytest.fixture
def keys(api: Api) -> tuple[str, str]:
    api.use_settings(make_settings(mock_settle_after_seconds=60))
    tenant_id, agent_key, tenant_key = create_tenant(api.store, "Demo", live=False, clock=api.clock)
    api.tenants.append(tenant_id)
    return agent_key, tenant_key


def demo(api: Api, keys: tuple[str, str], out: list[str], operator: Operator | None = None) -> Demo:
    agent_key, tenant_key = keys
    return Demo(
        client=api.client,
        agent_key=agent_key,
        tenant_key=tenant_key,
        say=out.append,
        wait=(operator or Operator(api, out)).wait,
        now=api.clock.now,
        patience=20,
    )


def test_journey_1_an_allow_becomes_a_payment(api: Api, keys: tuple[str, str]) -> None:
    out: list[str] = []

    payment = demo(api, keys, out).journey_1()

    assert payment["status"] == "scheduled"
    assert any("allow" in line for line in out)


def test_journey_2_a_step_up_is_approved_through_the_consent_link_then_paid(
    api: Api, keys: tuple[str, str]
) -> None:
    out: list[str] = []
    operator = Operator(api, out)

    payment = demo(api, keys, out, operator).journey_2()

    assert payment["status"] == "scheduled"
    assert any("step_up" in line for line in out)
    assert operator.used, "the CLI never printed a Consent link"


def test_journey_3_a_captured_payment_is_disputed_signed_off_and_verified(
    api: Api, keys: tuple[str, str]
) -> None:
    out: list[str] = []
    operator = Operator(api, out)

    dispute = demo(api, keys, out, operator).journey_3()

    assert dispute["status"] == "ready"
    assert dispute["sign_off"]["pack_hash"] == dispute["pack_hash"]
    assert any("verified" in line.lower() for line in out)


def test_the_journeys_run_one_after_another_and_never_print_a_key(
    api: Api, keys: tuple[str, str]
) -> None:
    out: list[str] = []
    run = demo(api, keys, out)

    run.journey_1()
    run.journey_2()
    run.journey_3()

    text = "\n".join(out)
    assert keys[0] not in text and keys[1] not in text


def test_a_failure_names_the_step(api: Api, keys: tuple[str, str]) -> None:
    out: list[str] = []
    wrong = ("apg_test_wrong", keys[1])

    with pytest.raises(StepFailed) as failed:
        demo(api, wrong, out).journey_1()

    assert failed.value.step == "Ask for a Decision"
    assert "401" in str(failed.value)


def test_waiting_gives_up_and_says_on_what(api: Api, keys: tuple[str, str]) -> None:
    out: list[str] = []
    never = Demo(
        client=api.client,
        agent_key=keys[0],
        tenant_key=keys[1],
        say=out.append,
        wait=lambda: None,  # nobody approves
        now=api.clock.now,
        patience=3,
    )

    with pytest.raises(StepFailed) as failed:
        never.journey_2()

    assert failed.value.step == "Wait for the Payer to approve"


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
