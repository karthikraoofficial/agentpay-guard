"""The live Intent eval runner, scored against a scripted gateway (no network)."""

from pathlib import Path

import pytest

from adapters.memory.model_gateway import ScriptedGateway
from core.ports import ModelTimeout
from evals.run_intent_evals import CASES, IntentCase, load_cases, main, run_cases
from tests.conftest import make_settings


def case(kind: str, acceptable: list[str], case_id: str = "c") -> IntentCase:
    return IntentCase.model_validate(
        {
            "id": case_id,
            "kind": kind,
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

    assert 28 <= len(cases) <= 40
    assert len({c.id for c in cases}) == len(cases)
    kinds = {c.kind for c in cases}
    assert kinds == {"match", "mismatch", "ambiguous", "injection"}
    assert sum(c.kind == "injection" for c in cases) >= 5
    for c in cases:
        assert c.acceptable, c.id
        if c.kind == "injection":
            assert "match" not in c.acceptable, c.id


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
