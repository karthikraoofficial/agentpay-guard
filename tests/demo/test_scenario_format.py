"""The Scenario file format (#273): one model, one committed schema, one catalogue."""

import json
from pathlib import Path
from typing import get_args

import pytest

from demo.scenario import SCHEMA, AgentTool, catalogue, schema_text
from guard_mcp.tools import TOOLS


def test_the_committed_schema_is_the_models() -> None:
    assert SCHEMA.read_text(encoding="utf-8") == schema_text(), (
        "demo/scenario.schema.json is stale: run `uv run python -m demo.scenario --write-schema`"
    )


def test_agent_steps_name_exactly_the_tools_guard_mcp_publishes() -> None:
    assert set(get_args(AgentTool)) == {tool.name for tool in TOOLS}


def test_a_file_must_be_named_after_its_scenario(tmp_path: Path) -> None:
    a1 = json.loads(Path("demo/scenarios/A1.json").read_text(encoding="utf-8"))
    (tmp_path / "A2.json").write_text(json.dumps(a1), encoding="utf-8")

    with pytest.raises(ValueError, match=r"name it A1.json"):
        catalogue(tmp_path)


def test_a_mandate_cant_both_start_later_and_have_expired(tmp_path: Path) -> None:
    b9 = json.loads(Path("demo/scenarios/B9.json").read_text(encoding="utf-8"))
    b9["setup"]["mandate"]["expired_days_ago"] = 1
    (tmp_path / "B9.json").write_text(json.dumps(b9), encoding="utf-8")

    with pytest.raises(ValueError, match="start later and have expired"):
        catalogue(tmp_path)


def test_an_agent_step_acts_only_as_an_agent_an_earlier_step_registered(tmp_path: Path) -> None:
    b6 = json.loads(Path("demo/scenarios/B6.json").read_text(encoding="utf-8"))
    b6["steps"][1]["as_agent"] = "Rogue"
    (tmp_path / "B6.json").write_text(json.dumps(b6), encoding="utf-8")

    with pytest.raises(ValueError, match="Rogue"):
        catalogue(tmp_path)


def test_money_in_a_scenario_is_integer_minor_units(tmp_path: Path) -> None:
    a1 = json.loads(Path("demo/scenarios/A1.json").read_text(encoding="utf-8"))
    a1["steps"][0]["amount"] = {"minor": 1250.0, "currency": "INR"}
    (tmp_path / "A1.json").write_text(json.dumps(a1), encoding="utf-8")

    with pytest.raises(ValueError, match="minor"):
        catalogue(tmp_path)


def test_a_scripted_intent_reply_is_one_verdict(tmp_path: Path) -> None:
    d1 = json.loads(Path("demo/scenarios/D1.json").read_text(encoding="utf-8"))
    d1["model_replies"] = [{"job": "intent_check", "reply": {"verdict": "probably"}}]
    (tmp_path / "D1.json").write_text(json.dumps(d1), encoding="utf-8")

    with pytest.raises(ValueError, match="verdict"):
        catalogue(tmp_path)


def test_each_intent_check_that_reaches_the_model_has_one_scripted_reply(tmp_path: Path) -> None:
    d1 = json.loads(Path("demo/scenarios/D1.json").read_text(encoding="utf-8"))
    d1["model_replies"] = []
    (tmp_path / "D1.json").write_text(json.dumps(d1), encoding="utf-8")

    with pytest.raises(ValueError, match=r"1 Intent check step.* 0 scripted"):
        catalogue(tmp_path)


def test_scripted_verdicts_skip_a_check_made_with_the_ai_off() -> None:
    scenarios = catalogue()

    assert scenarios["D5"].scripted_verdicts() == {}
    assert scenarios["D2"].scripted_verdicts() == {0: "mismatch"}
    assert scenarios["D6"].scripted_verdicts() == {0: "match"}


def test_a_cart_check_scripted_to_match_doesnt_lean_on_the_payers_history() -> None:
    # The live model can't see what "the usual" was, so it answers unknown (as the
    # Intent eval's ambiguous-same-as-last case expects) and the hosted run differs.
    vague = ("usual", "last time")
    leaning = [
        (scenario.id, step.instruction)
        for scenario in catalogue().values()
        for index, verdict in scenario.scripted_verdicts().items()
        if verdict == "match"
        and (step := scenario.steps[index]).instruction
        and any(word in step.instruction.lower() for word in vague)
    ]

    assert leaning == []


def test_a_reviewer_does_only_what_the_dispute_viewer_offers(tmp_path: Path) -> None:
    g1 = json.loads(Path("demo/scenarios/G1.json").read_text(encoding="utf-8"))
    reviewer = next(step for step in g1["steps"] if step["actor"] == "reviewer")
    reviewer["action"] = "approve"
    (tmp_path / "G1.json").write_text(json.dumps(g1), encoding="utf-8")

    with pytest.raises(ValueError, match="a reviewer step's action is one of"):
        catalogue(tmp_path)


def test_only_a_write_is_resent(tmp_path: Path) -> None:
    a1 = json.loads(Path("demo/scenarios/A1.json").read_text(encoding="utf-8"))
    a1["steps"].append({"actor": "agent", "tool": "get_decision", "sends": 2})
    (tmp_path / "A1.json").write_text(json.dumps(a1), encoding="utf-8")

    with pytest.raises(ValueError, match="only a tool that takes an idempotency key"):
        catalogue(tmp_path)


def test_only_a_read_waits(tmp_path: Path) -> None:
    g10 = json.loads(Path("demo/scenarios/G10.json").read_text(encoding="utf-8"))
    g10["steps"][-1]["until_status"] = ["withdrawn"]
    (tmp_path / "G10.json").write_text(json.dumps(g10), encoding="utf-8")

    with pytest.raises(ValueError, match="only a GET route can wait"):
        catalogue(tmp_path)


def test_only_a_decision_step_expects_an_outcome(tmp_path: Path) -> None:
    f2 = json.loads(Path("demo/scenarios/F2.json").read_text(encoding="utf-8"))
    f2["steps"][1]["expect_outcome"] = "allow"  # create_payment makes no Decision
    (tmp_path / "F2.json").write_text(json.dumps(f2), encoding="utf-8")

    with pytest.raises(ValueError, match="only a request_decision step expects an outcome"):
        catalogue(tmp_path)
