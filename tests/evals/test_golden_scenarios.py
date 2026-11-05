"""The scenario golden set (#136): past 200 Decision cases, every build-plan scenario
covered, and every prompt-injection case paired with a twin that proves the injected
text changes nothing."""

from typing import Any

from evals.run_evals import GOLDEN, Case, load_cases

SCENARIOS = {
    "clean_allow",
    "over_cap",
    "wrong_category",
    "intent_mismatch",
    "velocity_spike",
    "price_drift",
    "expired_mandate",
    "prompt_injection",
}
INJECTED = "-injected"


def decision_cases() -> list[Case]:
    files = [p for p in sorted(GOLDEN.glob("*.jsonl")) if (GOLDEN / f"{p.stem}_base.json").exists()]
    decision_files = [p for p in files if not p.stem.startswith(("disputes", "narrative"))]
    return [case for path in decision_files for case in load_cases(path)]


def scenario_cases() -> list[Case]:
    return load_cases(GOLDEN / "scenarios.jsonl")


def test_the_golden_set_has_200_decision_cases() -> None:
    assert len(decision_cases()) >= 200


def test_every_case_names_a_scenario_and_each_has_ten() -> None:
    cases = scenario_cases()
    counts = {name: sum(c.scenario == name for c in cases) for name in SCENARIOS}

    assert {c.scenario for c in cases} == SCENARIOS
    assert min(counts.values()) >= 10, counts


def _without_text(case: Case) -> dict[str, Any]:
    """The case's inputs with every Agent-side free-text field taken out."""
    request = dict(case.request)
    request["merchant"] = {k: v for k, v in request["merchant"].items() if k != "name"}
    request.pop("agent_reasoning", None)
    if request.get("line_items"):
        request["line_items"] = [
            {k: v for k, v in item.items() if k != "description"} for item in request["line_items"]
        ]
    data = case.model_dump(exclude={"id", "description", "scenario", "request"})
    return {**data, "request": request}


def test_each_injection_has_a_twin_that_differs_only_in_text_and_outcome_not_at_all() -> None:
    by_id = {c.id: c for c in scenario_cases()}
    injected = [c for c in by_id.values() if c.scenario == "prompt_injection"]

    assert len(injected) >= 10
    for case in injected:
        assert case.id.endswith(INJECTED), case.id
        twin = by_id[case.id.removesuffix(INJECTED)]
        assert twin.scenario != "prompt_injection"
        assert _without_text(case) == _without_text(twin), case.id
        assert case.request != twin.request, f"{case.id} has no injected text"
        assert case.expected == twin.expected, case.id
        verdict = (case.signals.get("intent") or {}).get("verdict")
        assert verdict in {"mismatch", "unknown"}, case.id
