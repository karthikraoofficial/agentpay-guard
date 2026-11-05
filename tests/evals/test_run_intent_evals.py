"""The live Intent eval runner, scored against a scripted gateway (no network)."""

from pathlib import Path

import pytest

from adapters.memory.model_gateway import ScriptedGateway
from core.ports import ModelTimeout, ModelUnavailable
from evals.run_intent_evals import (
    CASES,
    TECHNIQUES,
    IntentCase,
    load_cases,
    main,
    run_cases,
)
from tests.conftest import make_settings


def case(
    kind: str, acceptable: list[str], case_id: str = "c", technique: str | None = None
) -> IntentCase:
    if kind == "injection" and technique is None:
        technique = "direct"
    return IntentCase.model_validate(
        {
            "id": case_id,
            "kind": kind,
            "technique": technique,
            "payer_instruction": "Buy running shoes",
            "merchant": {"id": "m", "name": "SportsHub", "mcc": "5661"},
            "line_items": [
                {
                    "description": "Shoes",
                    "quantity": 2,
                    "unit_amount": {"minor": 1_000_00, "currency": "INR"},
                }
            ],
            "acceptable": acceptable,
        }
    )


def test_the_committed_set_has_about_30_labelled_cases_with_injection_attempts() -> None:
    cases = load_cases(CASES)

    assert 40 <= len(cases) <= 50
    assert len({c.id for c in cases}) == len(cases)
    kinds = {c.kind for c in cases}
    assert kinds == {"match", "mismatch", "ambiguous", "injection"}
    assert sum(c.kind == "injection" for c in cases) >= 16
    for c in cases:
        assert c.acceptable, c.id
        if c.kind == "injection":
            assert "match" not in c.acceptable, c.id


def test_every_injection_names_its_technique_and_each_technique_has_cases() -> None:
    injections = [c for c in load_cases(CASES) if c.kind == "injection"]

    techniques = [c.technique for c in injections]
    assert set(techniques) == set(TECHNIQUES)
    assert min(techniques.count(t) for t in TECHNIQUES) >= 2


def test_only_injection_cases_carry_a_technique() -> None:
    with pytest.raises(ValueError, match="technique"):
        case("match", ["match"], technique="direct")
    with pytest.raises(ValueError, match="technique"):
        IntentCase.model_validate(case("injection", ["unknown"]).model_dump() | {"technique": None})


def test_injection_cases_are_scored_by_technique() -> None:
    cases = [
        case("injection", ["unknown", "mismatch"], "a", "role_play"),
        case("injection", ["unknown", "mismatch"], "b", "role_play"),
        case("injection", ["unknown", "mismatch"], "c", "encoded"),
        case("injection", ["unknown", "mismatch"], "e", "encoded"),
        case("match", ["match"], "d"),
    ]
    gateway = ScriptedGateway("MATCH", "UNKNOWN", "MISMATCH", ModelUnavailable("400"), "MATCH")

    report = run_cases(cases, gateway, make_settings())

    scores = {t: (s.cases, s.held, s.false_match, s.failed) for t, s in report.by_technique.items()}
    assert scores == {"role_play": (2, 1, 1, 0), "encoded": (2, 1, 0, 1)}
    assert report.injection_false_match
    assert report.injection_unanswered == 1


def test_a_failed_call_never_counts_as_holding() -> None:
    """A call that fails is an unknown verdict, as in production, but proves nothing:
    with no credit every case would otherwise look held."""
    cases = [case("injection", ["unknown", "mismatch"], f"i{n}") for n in range(3)]
    gateway = ScriptedGateway(ModelTimeout("slow"), ModelUnavailable("400"), "UNKNOWN")

    report = run_cases(cases, gateway, make_settings())

    assert (report.by_technique["direct"].held, report.injection_unanswered) == (1, 2)
    assert not report.injection_false_match


def test_the_command_exits_3_when_an_injection_case_gets_no_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import evals.run_intent_evals as runner

    cases = tmp_path / "cases.jsonl"
    cases.write_text(case("injection", ["unknown"]).model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(runner, "get_settings", lambda: make_settings(anthropic_api_key="sk-t"))
    monkeypatch.setattr(runner, "ClaudeGateway", lambda *_: ScriptedGateway(ModelUnavailable("x")))

    assert main(["--cases", str(cases)]) == 3
    assert "got no answer" in capsys.readouterr().err


def test_the_quoted_total_is_the_sum_of_the_line_items() -> None:
    assert case("match", ["match"]).content().quoted_total.minor == 2_000_00


def test_false_matches_and_false_mismatches_are_counted() -> None:
    cases = [
        case("match", ["match"], "ok"),
        case("mismatch", ["mismatch"], "fm"),  # model says MATCH: a false match
        case("match", ["match"], "fx"),  # model says MISMATCH: a false mismatch
        case("match", ["match"], "slow"),  # timeout: unknown, a miss but neither false
    ]
    gateway = ScriptedGateway("MATCH", "MATCH", "MISMATCH", ModelTimeout("slow"))

    report = run_cases(cases, gateway, make_settings())

    assert (report.total, report.correct) == (4, 1)
    assert (report.false_match, report.false_mismatch) == (1, 1)
    assert [r.case_id for r in report.misses] == ["fm", "fx", "slow"]
    assert report.misses[-1].failure_cause == "timeout"
    assert report.tokens_in == 3 * 120 and report.cost_usd_micros == 3 * 13
    assert not report.injection_false_match


def test_a_false_match_on_an_injection_case_fails_the_run() -> None:
    report = run_cases(
        [case("injection", ["unknown", "mismatch"])], ScriptedGateway("MATCH"), make_settings()
    )

    assert report.injection_false_match


def test_the_command_refuses_to_run_without_an_api_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import evals.run_intent_evals as runner

    monkeypatch.setattr(runner, "get_settings", lambda: make_settings(anthropic_api_key=None))

    assert main([]) == 2
    assert "AGENTPAY_ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_the_eval_set_is_not_collected_by_pytest() -> None:
    assert not list(Path(CASES).parent.glob("test_*.py"))
