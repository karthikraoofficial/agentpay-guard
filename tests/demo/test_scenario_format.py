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


def test_money_in_a_scenario_is_integer_minor_units(tmp_path: Path) -> None:
    a1 = json.loads(Path("demo/scenarios/A1.json").read_text(encoding="utf-8"))
    a1["steps"][0]["amount"] = {"minor": 1250.0, "currency": "INR"}
    (tmp_path / "A1.json").write_text(json.dumps(a1), encoding="utf-8")

    with pytest.raises(ValueError, match="minor"):
        catalogue(tmp_path)
