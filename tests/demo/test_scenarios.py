"""Every Scenario file in the catalogue, run through the step engine against the in-process
app, ends with its expected result (#273). This is the one place a journey is proven.

Each time a run waits, the test plays the worker and the people, as the demo CLI tests do.
"""

import pytest

from core.ports import ModelSpendCapReached
from demo.__main__ import AS_SCENARIO, main, run_scenario
from demo.engine import Run, play
from demo.scenario import Scenario, catalogue
from tests.api.conftest import Api
from tests.conftest import make_settings
from tests.demo.conftest import clients
from tests.demo.operator import Operator

pytestmark = pytest.mark.anyio

SCENARIOS = list(catalogue().values())


def script_the_model(api: Api, scenario: Scenario) -> None:
    """The fake model gateway answers each Intent check with the Scenario's scripted
    verdict, so CI makes no paid call."""
    api.gateway.script.extend(v.upper() for v in scenario.scripted_verdicts().values())


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
async def test_each_scenario_ends_with_its_expected_result(
    api: Api, keys: tuple[str, str], scenario: Scenario
) -> None:
    operator = Operator(api, [])
    script_the_model(api, scenario)
    async with clients(api, keys) as (agent, tenant):
        # In CI the engine plays Asha; on the demo page a visitor does.
        run = Run(scenario, agent=agent, tenant=tenant, now=api.clock.now, people="engine")

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


async def test_e6_a_double_tap_records_one_approval(api: Api, keys: tuple[str, str]) -> None:
    operator = Operator(api, [])
    async with clients(api, keys) as (agent, tenant):
        run = Run(catalogue()["E6"], agent=agent, tenant=tenant, now=api.clock.now, people="engine")

        cards = await play(run, wait=operator.wait, patience=20)

    tapped = next(card for card in cards if card.actor == "payer")
    assert tapped.consent is not None and tapped.consent.status == "approved"
    assert tapped.call is not None and tapped.call.response["taps"] == 2
    assert tapped.call.response["distinct_answers"] == 1


async def test_f3_a_resent_payment_request_makes_one_payment(
    api: Api, keys: tuple[str, str]
) -> None:
    operator = Operator(api, [])
    async with clients(api, keys) as (agent, tenant):
        run = Run(catalogue()["F3"], agent=agent, tenant=tenant, now=api.clock.now, people="engine")

        cards = await play(run, wait=operator.wait, patience=20)

    paid = next(card for card in cards if card.call and card.call.name == "create_payment")
    assert paid.call is not None and paid.call.response["sends"] == 2
    assert paid.call.response["distinct_answers"] == 1
    payment_id = paid.call.response["answer"]["payment_id"]
    assert payment_id.startswith("pmt_")
    read = cards[-1].call
    assert read is not None and read.response["payment_id"] == payment_id
    assert cards[-1].mismatches == []


async def test_the_cli_prints_a_resent_calls_one_answer(api: Api, keys: tuple[str, str]) -> None:
    out: list[str] = []
    operator = Operator(api, out)
    async with clients(api, keys) as (agent, tenant):
        code = await run_scenario(
            catalogue()["F3"],
            agent=agent,
            tenant=tenant,
            say=out.append,
            wait=operator.wait,
            now=api.clock.now,
            patience=20,
        )

    assert code == 0
    assert any(
        line.startswith("  - create_payment: pmt_")
        and line.endswith("scheduled (sent 2 times, 1 distinct answer)")
        for line in out
    ), "\n".join(out)


async def test_b6_asks_as_the_agent_the_run_registered(api: Api, keys: tuple[str, str]) -> None:
    operator = Operator(api, [])
    async with clients(api, keys) as (agent, tenant):
        run = Run(catalogue()["B6"], agent=agent, tenant=tenant, now=api.clock.now, people="engine")

        cards = await play(run, wait=operator.wait, patience=20)

    mandate, registered, decided = (card.call for card in cards)
    assert mandate is not None and registered is not None and decided is not None
    dealhunter = registered.response["agent_id"]
    assert registered.response["name"] == "DealHunter"
    assert decided.request is not None and decided.request["agent_id"] == dealhunter
    assert dealhunter not in mandate.response["allowed_agent_ids"]


async def test_e5_shows_the_refusal_on_its_card(api: Api, keys: tuple[str, str]) -> None:
    operator = Operator(api, [])
    async with clients(api, keys) as (agent, tenant):
        run = Run(catalogue()["E5"], agent=agent, tenant=tenant, now=api.clock.now, people="engine")

        cards = await play(run, wait=operator.wait, patience=20)

    refused = next(card for card in cards if card.refused)
    assert refused.actor == "agent"
    assert refused.refused == "INSUFFICIENT_SCOPE"
    assert refused.call is not None and refused.call.kind == "route"


async def test_the_cli_prints_the_consent_link_and_waits_for_asha(
    api: Api, keys: tuple[str, str]
) -> None:
    out: list[str] = []
    operator = Operator(api, out)  # approves through the link the CLI prints
    async with clients(api, keys) as (agent, tenant):
        code = await run_scenario(
            catalogue()["E1"],
            agent=agent,
            tenant=tenant,
            say=out.append,
            wait=operator.wait,
            now=api.clock.now,
            patience=20,
        )

    assert code == 0, "\n".join(out)
    assert operator.used, "the CLI never printed a Consent link"
    assert any("approved" in line for line in out)


async def test_d2_shows_the_mismatch_on_the_card_and_on_ashas_consent_screen(
    api: Api, keys: tuple[str, str]
) -> None:
    scenario = catalogue()["D2"]
    script_the_model(api, scenario)
    async with clients(api, keys) as (agent, tenant):
        # A visitor plays Asha: stop while she is looking at her screen.
        run = Run(scenario, agent=agent, tenant=tenant, now=api.clock.now)
        cards = [await run.next() for _ in range(4)]

        checked = next(card for card in cards if card.intent)
        asked = cards[-1]
        assert asked.waiting and asked.link is not None
        screen = await tenant.get(
            "/v1/consent-link", headers={"Consent-Link": asked.link.split("#", 1)[1]}
        )

    assert checked.intent is not None
    assert (checked.intent.verdict, checked.intent.scripted) == ("mismatch", "mismatch")
    assert not checked.intent.differs
    assert screen.json()["intent_verdict"] == "mismatch"
    assert [r["code"] for r in screen.json()["reasons"]] == ["INTENT_MISMATCH"]
    assert "doesn't match what you asked" in screen.json()["reasons"][0]["text"]


async def test_a_live_verdict_unlike_the_scripted_one_is_flagged_and_the_run_carries_on(
    api: Api, keys: tuple[str, str]
) -> None:
    api.gateway.script.append("MISMATCH")  # D1 scripts a match
    operator = Operator(api, [])
    async with clients(api, keys) as (agent, tenant):
        run = Run(catalogue()["D1"], agent=agent, tenant=tenant, now=api.clock.now)

        cards = await play(run, wait=operator.wait, patience=20)

    checked = cards[1]
    assert checked.intent is not None
    assert (checked.intent.verdict, checked.intent.scripted) == ("mismatch", "match")
    assert checked.intent.differs
    decided = cards[-1]
    assert decided.decision is not None
    assert decided.decision.reason_codes == ["INTENT_MISMATCH"]
    assert any("live model said mismatch" in m for m in decided.mismatches)


async def test_a_capped_check_shows_unknown_with_its_cause(api: Api, keys: tuple[str, str]) -> None:
    api.gateway.script.append(ModelSpendCapReached("daily cap"))
    operator = Operator(api, [])
    async with clients(api, keys) as (agent, tenant):
        run = Run(catalogue()["D1"], agent=agent, tenant=tenant, now=api.clock.now)

        cards = await play(run, wait=operator.wait, patience=20)

    checked = cards[1]
    assert checked.intent is not None
    assert (checked.intent.verdict, checked.intent.failure_cause) == ("unknown", "capped")
    assert checked.intent.differs


async def test_the_cli_prints_the_cart_checks_verdict(api: Api, keys: tuple[str, str]) -> None:
    scenario = catalogue()["D4"]
    out: list[str] = []
    script_the_model(api, scenario)
    async with clients(api, keys) as (agent, tenant):
        code = await run_scenario(
            scenario,
            agent=agent,
            tenant=tenant,
            say=out.append,
            wait=Operator(api, []).wait,
            now=api.clock.now,
            patience=20,
        )

    assert code == 0, "\n".join(out)
    assert any("check_intent: unknown (scripted: unknown)" in line for line in out), out


async def test_g1_builds_a_strong_pack_the_reviewer_signs_and_verify_pack_checks(
    api: Api, keys: tuple[str, str]
) -> None:
    scenario = catalogue()["G1"]
    operator = Operator(api, [])
    script_the_model(api, scenario)
    async with clients(api, keys) as (agent, tenant):
        run = Run(scenario, agent=agent, tenant=tenant, now=api.clock.now, people="engine")
        cards = await play(run, wait=operator.wait, patience=20)
        verification = await run.verify()

    built = next(card for card in cards if card.dispute and card.dispute.status == "in_review")
    assert built.dispute is not None
    assert (built.dispute.pack_version, built.dispute.evidence_strength) == (1, "strong")
    assert built.dispute.narrative_source == "model"
    signed = next(card for card in cards if card.actor == "reviewer")
    assert signed.link is not None and "/dispute#" in signed.link
    assert signed.dispute is not None and signed.dispute.status == "ready"
    assert signed.dispute.signed_off_version == 1
    checked = cards[-1]
    assert checked.pack_check is not None and checked.pack_check.clean
    assert [(v.version, v.signed_off) for v in checked.pack_check.versions] == [(1, True)]
    assert verification is not None and verification.pack is not None
    assert verification.pack.clean


async def test_g8_flags_a_narrative_written_by_code(api: Api, keys: tuple[str, str]) -> None:
    operator = Operator(api, [])
    async with clients(api, keys) as (agent, tenant):
        run = Run(catalogue()["G8"], agent=agent, tenant=tenant, now=api.clock.now, people="engine")
        cards = await play(run, wait=operator.wait, patience=20)

    built = next(card for card in cards if card.dispute and card.dispute.status == "in_review")
    assert built.dispute is not None
    assert built.dispute.narrative_source == "template"
    assert built.dispute.narrative_note == "model narrative unavailable"


async def test_a_visitor_reviewer_signs_off_in_the_dispute_viewer(
    api: Api, keys: tuple[str, str]
) -> None:
    operator = Operator(api, [])
    async with clients(api, keys) as (agent, tenant):
        run = Run(catalogue()["H7"], agent=agent, tenant=tenant, now=api.clock.now)
        card = await run.next()
        while card.actor != "reviewer":
            if card.waiting:
                operator.wait()
            card = await run.next()
        assert card.waiting and card.link is not None
        assert (await run.next(card.step)).waiting  # nobody has signed yet

        token = card.link.split("#", 1)[1]
        signed = await tenant.post(
            "/v1/review-link/sign-off",
            headers={"Review-Link": token, "Idempotency-Key": "visitor-sign-off"},
            json={"pack_version": 1},
        )
        assert signed.status_code == 200, signed.text
        after = await run.next(card.step)

    assert not after.waiting
    assert after.dispute is not None and after.dispute.status == "ready"


async def test_journey_3_on_the_cli_is_scenario_g1(api: Api, keys: tuple[str, str]) -> None:
    out: list[str] = []
    operator = Operator(api, out)  # signs off through the Review link the CLI prints
    script_the_model(api, catalogue()["G1"])
    async with clients(api, keys) as (agent, tenant):
        code = await run_scenario(
            catalogue()[AS_SCENARIO["3"]],
            agent=agent,
            tenant=tenant,
            say=out.append,
            wait=operator.wait,
            now=api.clock.now,
            patience=20,
        )

    assert code == 0, "\n".join(out)
    assert AS_SCENARIO["3"] == "G1"
    assert operator.used, "the CLI never printed a Review link"
    assert any("strength strong" in line for line in out), out
    assert any("verify-pack: clean" in line for line in out), out
