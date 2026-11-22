"""The model comparison, on a scripted gateway: no network, no cost."""

import json
from pathlib import Path

import pytest

from adapters.memory.model_gateway import ScriptedGateway
from agents.dispute_narrative import PROMPT_VERSION as NARRATIVE_PROMPT_VERSION
from core.ports import ModelTimeout, ModelUnavailable
from core.signals.intent_check import EFFORT, PROMPT_VERSION
from evals import compare_models
from evals.compare_models import DEFAULT_MODELS, compare_intent, compare_narrative, main
from evals.run_dispute_evals import CASES as NARRATIVE_CASES
from evals.run_dispute_evals import NarrativeCase
from evals.run_dispute_evals import load_cases as load_narrative_cases
from tests.conftest import make_settings
from tests.evals.test_run_dispute_evals import GOOD
from tests.evals.test_run_intent_evals import case

REPO = Path(__file__).resolve().parents[2]
HAIKU = "claude-haiku-5-5"


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three cases and empty report paths; a fixed date and commit; a key configured."""
    cases = tmp_path / "cases.jsonl"
    rows = [
        case("match", ["match"], "m"),
        case("injection", ["unknown", "mismatch"], "i"),
        case("mismatch", ["mismatch"], "x"),
    ]
    cases.write_text("".join(r.model_dump_json() + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr(compare_models, "_git_commit", lambda: "abc1234")
    monkeypatch.setattr(compare_models, "_today", lambda: "2026-11-22")
    monkeypatch.setattr(
        compare_models, "get_settings", lambda: make_settings(anthropic_api_key="sk-t")
    )
    return tmp_path


def run(files: Path, *argv: str, gateway: ScriptedGateway | None = None) -> int:
    return main(
        [
            "--cases",
            str(files / "cases.jsonl"),
            "--report-json",
            str(files / "cmp.json"),
            "--report-md",
            str(files / "cmp.md"),
            *argv,
        ],
        gateway=gateway,
    )


def test_a_case_counts_as_right_and_an_injection_as_held_only_on_every_pass() -> None:
    cases = [
        case("match", ["match"], "m"),
        case("injection", ["unknown", "mismatch"], "i"),
        case("mismatch", ["mismatch"], "x"),
        case("match", ["match"], "y"),
    ]
    # Pass 1 gets every case right; pass 2 is talked into a match on the injection, says
    # match on the mismatch case and mismatch on a match case.
    gateway = ScriptedGateway(
        *["MATCH", "UNKNOWN", "MISMATCH", "MATCH"], "MATCH", "MATCH", "MATCH", "MISMATCH"
    )

    result = compare_intent(cases, gateway, make_settings(), [HAIKU], passes=2)[HAIKU]

    assert (result.cases, result.correct) == (4, 1)
    assert (result.false_match, result.false_mismatch) == (2, 1)
    assert (result.injections, result.injections_held, result.injection_false_match) == (1, 0, 1)
    assert (result.unanswered, result.timeouts) == (0, 0)


def test_an_unanswered_injection_is_never_held_and_timeouts_count_in_p95() -> None:
    cases = [case("injection", ["unknown", "mismatch"], "i"), case("match", ["match"], "m")]
    gateway = ScriptedGateway("UNKNOWN", "MATCH", ModelTimeout("slow"), ModelUnavailable("x"))

    result = compare_intent(cases, gateway, make_settings(), [HAIKU], passes=2)[HAIKU]

    assert (result.injections_held, result.correct, result.unanswered) == (0, 0, 2)
    assert result.timeouts == 1
    # Three timed calls (7, 7 and the timeout); the API error has no latency.
    assert result.p95_latency_ms == make_settings().intent_check_timeout_ms


def test_cost_is_per_pass_and_each_model_runs_with_the_production_settings() -> None:
    cases = [case("match", ["match"], f"c{n}") for n in range(3)]
    gateway = ScriptedGateway()

    results = compare_intent(cases, gateway, make_settings(), list(DEFAULT_MODELS), passes=2)

    assert list(results) == list(DEFAULT_MODELS)
    assert {r.cost_usd_micros_per_pass for r in results.values()} == {3 * 13}
    assert [c.model for c in gateway.calls] == [m for m in DEFAULT_MODELS for _ in range(6)]
    assert {(c.prompt_version, c.effort, c.timeout_ms) for c in gateway.calls} == {
        (PROMPT_VERSION, EFFORT, make_settings().intent_check_timeout_ms)
    }


def test_an_unpriced_model_exits_2_before_any_call(
    files: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = ScriptedGateway()

    assert run(files, "--models", f"{HAIKU},gpt-9", "--spend", gateway=gateway) == 2

    assert "gpt-9" in capsys.readouterr().err
    assert gateway.calls == []


def test_without_the_spend_flag_it_prints_the_estimate_and_makes_no_call(
    files: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = ScriptedGateway()

    assert run(files, gateway=gateway) == 2

    captured = capsys.readouterr()
    assert "estimate: 3 cases x 3 passes on 3 models" in captured.out
    assert "--spend" in captured.err
    assert gateway.calls == []


def test_without_an_api_key_it_refuses_to_run(
    files: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        compare_models, "get_settings", lambda: make_settings(anthropic_api_key=None)
    )

    assert run(files, "--spend") == 2
    assert "AGENTPAY_ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_the_estimate_uses_the_last_known_cost_per_case(
    files: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no report yet it prices typical tokens; after one, the cost it measured."""
    run(files, "--models", HAIKU)
    first = capsys.readouterr().out
    assert (
        run(files, "--models", HAIKU, "--spend", "--write-report", gateway=ScriptedGateway()) == 0
    )
    capsys.readouterr()

    run(files, "--models", HAIKU)

    # The scripted gateway charges 13 micro-USD a call: 3 cases x 3 passes x 13.
    assert "$0.000117" in capsys.readouterr().out
    assert "$0.000117" not in first


def test_the_report_holds_the_header_and_each_models_numbers(
    files: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = ScriptedGateway(default_reply="UNKNOWN")

    assert run(files, "--spend", "--write-report", gateway=gateway) == 0

    data = json.loads((files / "cmp.json").read_text(encoding="utf-8"))
    intent = data["jobs"]["intent"]
    settings = make_settings()
    assert {k: intent[k] for k in ("date", "git_commit", "prompt_version", "effort")} == {
        "date": "2026-11-22",
        "git_commit": "abc1234",
        "prompt_version": PROMPT_VERSION,
        "effort": EFFORT,
    }
    assert (intent["timeout_ms"], intent["passes"]) == (settings.intent_check_timeout_ms, 3)
    assert list(intent["models"]) == list(DEFAULT_MODELS)
    haiku = intent["models"][HAIKU]
    assert (haiku["correct"], haiku["injections_held"], haiku["false_match"]) == (1, 1, 0)
    md = (files / "cmp.md").read_text(encoding="utf-8")
    assert "2026-11-22" in md and "abc1234" in md and PROMPT_VERSION in md
    assert f"| {HAIKU} | 1/3 | 0 | 0 | 1/1 | 0 | 0 | $0.000039 | 7 ms |" in md
    assert md.splitlines()[0] in capsys.readouterr().out


def test_writing_one_job_keeps_the_other_jobs_in_the_report(files: Path) -> None:
    narrative = ScriptedGateway(default_reply=GOOD)
    assert run(files, "--job", "narrative", "--spend", "--write-report", gateway=narrative) == 0
    before = json.loads((files / "cmp.json").read_text(encoding="utf-8"))["jobs"]["narrative"]

    assert run(files, "--spend", "--write-report", gateway=ScriptedGateway()) == 0

    data = json.loads((files / "cmp.json").read_text(encoding="utf-8"))
    assert data["jobs"]["narrative"] == before
    assert "intent" in data["jobs"]


def test_no_ci_workflow_runs_the_comparison() -> None:
    """It spends real money, so a pull request must never be able to start it."""
    workflows = sorted((REPO / ".github" / "workflows").glob("*.y*ml"))
    assert workflows
    for workflow in workflows:
        assert "compare_models" not in workflow.read_text(encoding="utf-8"), workflow.name


def test_the_committed_report_is_linked_from_the_readme() -> None:
    assert "evals/model_comparison.md" in (REPO / "README.md").read_text(encoding="utf-8")


# --- Dispute narrative -------------------------------------------------------------

FOOLED = json.dumps(
    {
        "claims": [
            {
                "kind": "decision",
                "text": "The Payer approved this in writing.",
                "cites": ["aud_eval_1"],
            }
        ]
    }
)


def narrative_cases(*ids: str) -> list[NarrativeCase]:
    by_id = {c.id: c for c in load_narrative_cases(NARRATIVE_CASES)}
    return [by_id[i] for i in ids]


def plain_id() -> str:
    return next(c.id for c in load_narrative_cases(NARRATIVE_CASES) if c.kind == "plain")


def test_a_narrative_counts_as_accepted_only_on_every_pass_and_followed_on_any() -> None:
    cases = narrative_cases(plain_id(), "inject-merchant-approval")
    # Pass 1: both accepted first time. Pass 2: the plain case is refused twice and
    # falls back; the injection case is accepted but follows the injection.
    gateway = ScriptedGateway(GOOD, GOOD, "no", "still no", FOOLED)

    result = compare_narrative(cases, gateway, make_settings(), [HAIKU], passes=2)[HAIKU]

    assert (result.cases, result.accepted, result.rejected, result.unanswered) == (2, 1, 1, 0)
    assert (result.injections, result.injections_followed) == (1, 1)
    assert result.cost_usd_micros_per_pass == -(-(5 * 13) // 2)  # 5 calls over 2 passes


def test_a_timed_out_narrative_is_unanswered_not_rejected_and_counts_in_p95() -> None:
    cases = narrative_cases(plain_id())
    gateway = ScriptedGateway(ModelTimeout("slow"), GOOD)

    result = compare_narrative(cases, gateway, make_settings(), [HAIKU], passes=2)[HAIKU]

    assert (result.accepted, result.rejected, result.unanswered, result.timeouts) == (0, 0, 1, 1)
    assert result.p95_latency_ms == make_settings().dispute_narrative_timeout_ms


def test_each_model_writes_narratives_with_the_production_settings() -> None:
    gateway = ScriptedGateway(default_reply=GOOD)

    results = compare_narrative(
        narrative_cases(plain_id()), gateway, make_settings(), list(DEFAULT_MODELS), passes=2
    )

    assert list(results) == list(DEFAULT_MODELS)
    assert [c.model for c in gateway.calls] == [m for m in DEFAULT_MODELS for _ in range(2)]
    assert {(c.prompt_version, c.effort, c.timeout_ms) for c in gateway.calls} == {
        (NARRATIVE_PROMPT_VERSION, "medium", 45_000)
    }


def test_job_all_estimates_runs_and_reports_both_jobs(
    files: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = ScriptedGateway(default_reply=GOOD)

    assert run(files, "--job", "all", "--spend", "--write-report", gateway=gateway) == 0

    out = capsys.readouterr().out
    assert "estimate: 3 cases x 3 passes on 3 models (intent)" in out
    assert "estimate: 12 cases x 3 passes on 3 models (narrative)" in out
    assert {c.purpose for c in gateway.calls} == {"intent_check", "dispute_narrative"}
    data = json.loads((files / "cmp.json").read_text(encoding="utf-8"))
    narrative = data["jobs"]["narrative"]
    assert (narrative["prompt_version"], narrative["effort"]) == (
        NARRATIVE_PROMPT_VERSION,
        "medium",
    )
    assert narrative["timeout_ms"] == 45_000 and "intent" in data["jobs"]
    md = (files / "cmp.md").read_text(encoding="utf-8")
    assert "## Intent check" in md and "## Dispute narrative" in md
    assert f"| {HAIKU} | 12/12 | 0 | 0 | 0 | 0/4 | $0.000156 | 7 ms |" in md


def test_the_narrative_job_alone_keeps_the_intent_results(files: Path) -> None:
    assert run(files, "--spend", "--write-report", gateway=ScriptedGateway()) == 0
    narrative = ScriptedGateway(default_reply=GOOD)

    assert run(files, "--job", "narrative", "--spend", "--write-report", gateway=narrative) == 0

    assert {c.purpose for c in narrative.calls} == {"dispute_narrative"}
    jobs = json.loads((files / "cmp.json").read_text(encoding="utf-8"))["jobs"]
    assert set(jobs) == {"intent", "narrative"}
