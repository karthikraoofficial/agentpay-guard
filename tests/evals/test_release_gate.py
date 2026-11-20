import json
import shutil
from pathlib import Path

import pytest
import yaml

from evals.release_gate import main, run_gate
from evals.run_evals import GOLDEN, PackPin
from tests.packs import PACKS, release_test_pack

ACTIVE = PackPin(id="default", version="1.5.0")
CANDIDATE = PackPin(id="default", version="1.6.0")
# Decision and approval golden cases today: phase1 44, phase2 36, scenarios 117, approval 6.
DECISION_CASES = 203


def _candidate(tmp_path: Path, changes: dict[str, object]) -> Path:
    """A packs directory where default@1.6.0 is default@1.5.0 with `changes`, inactive."""
    return release_test_pack(tmp_path / "packs", "1.6.0", changes, based_on="1.5.0", active=False)


def test_a_candidate_identical_to_the_active_pack_passes(tmp_path: Path) -> None:
    packs = _candidate(tmp_path, {})

    verdict = run_gate(packs, GOLDEN, ACTIVE, CANDIDATE)

    assert verdict.passed
    assert verdict.changes == []
    assert verdict.compared == DECISION_CASES


# INR 1,000 and up is high: phase2's allow-basic (INR 1,250, low on 1.5.0) steps up.
LOWER_INR_THRESHOLD = {"currencies": {"INR": {"medium_from": 50000, "step_up_threshold": 100000}}}


def test_an_undeclared_tightening_fails_and_shows_both_answers(tmp_path: Path) -> None:
    packs = _candidate(tmp_path, LOWER_INR_THRESHOLD)

    verdict = run_gate(packs, GOLDEN, ACTIVE, CANDIDATE)

    assert not verdict.passed
    key = ("phase2.jsonl", "allow-basic")
    change = next(c for c in verdict.changes if (c.file, c.case_id) == key)
    assert (change.active.outcome, change.active.risk_tier) == ("allow", "low")
    assert (change.candidate.outcome, change.candidate.risk_tier) == ("step_up", "high")
    # High now, and the base cites no Intent check, which 1.5.0 asks for from medium.
    assert change.candidate.reason_codes == ("AMOUNT_ABOVE_STEP_UP_THRESHOLD", "INTENT_MISSING")
    assert verdict.high_risk_loosenings == []


def test_the_verdict_gives_the_step_up_rate_on_each_pack(tmp_path: Path) -> None:
    same = run_gate(_candidate(tmp_path / "same", {}), GOLDEN, ACTIVE, CANDIDATE)
    stricter = run_gate(
        _candidate(tmp_path / "stricter", LOWER_INR_THRESHOLD), GOLDEN, ACTIVE, CANDIDATE
    )

    assert 0 < same.step_up_rate.active == same.step_up_rate.candidate < 1
    assert stricter.step_up_rate.active == same.step_up_rate.active
    assert stricter.step_up_rate.candidate > stricter.step_up_rate.active


def test_a_loosening_on_a_case_high_on_the_active_pack_is_a_high_risk_loosening(
    tmp_path: Path,
) -> None:
    # INR 20,000 and up is high: phase2's threshold-at (INR 5,000, high on 1.5.0, with a
    # matching Intent check) is medium and allowed.
    packs = _candidate(tmp_path, {"currencies": {"INR": {"step_up_threshold": 2000000}}})

    verdict = run_gate(packs, GOLDEN, ACTIVE, CANDIDATE)

    assert not verdict.passed
    loosened = {(c.file, c.case_id) for c in verdict.high_risk_loosenings}
    assert ("phase2.jsonl", "threshold-at") in loosened
    change = next(c for c in verdict.high_risk_loosenings if c.case_id == "threshold-at")
    assert (change.active.outcome, change.active.risk_tier) == ("step_up", "high")
    assert (change.candidate.outcome, change.candidate.risk_tier) == ("allow", "medium")


def _rules_without(rule_id: str) -> list[dict[str, object]]:
    pack = yaml.safe_load((PACKS / "default" / "1.5.0.yaml").read_text(encoding="utf-8"))
    return [r | {"enabled": r["rule_id"] != rule_id} for r in pack["rules"]]


def test_an_approval_that_goes_through_where_it_was_blocked_is_a_high_risk_loosening(
    tmp_path: Path,
) -> None:
    # Without the Monthly cap rule, approving a payment that would pass the cap goes through.
    packs = _candidate(tmp_path, {"rules": _rules_without("monthly_cap")})

    verdict = run_gate(packs, GOLDEN, ACTIVE, CANDIDATE)

    change = next(c for c in verdict.high_risk_loosenings if c.case_id == "approve-cap-used-up")
    assert (change.active.resolution, change.candidate.resolution) == ("blocked", "approved")


def _golden_with_candidate_file(tmp_path: Path, relabel: str | None = None) -> Path:
    """The real golden set plus `candidate.jsonl`: phase2's cases pinned to default@1.6.0,
    with `relabel`'s label turned into a wrong block."""
    golden = tmp_path / "golden"
    shutil.copytree(GOLDEN, golden)
    base = json.loads((GOLDEN / "phase2_base.json").read_text(encoding="utf-8"))
    base["pack"] = {"id": "default", "version": "1.6.0"}
    (golden / "candidate_base.json").write_text(json.dumps(base), encoding="utf-8")
    rows = [json.loads(line) for line in (GOLDEN / "phase2.jsonl").read_text().splitlines()]
    for row in rows:
        if row["id"] == relabel:
            row["expected"] = {"outcome": "block", "reason_codes": ["MCC_NOT_ALLOWED"]}
    (golden / "candidate.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return golden


def test_a_candidate_that_breaks_its_own_golden_file_fails(tmp_path: Path) -> None:
    packs = _candidate(tmp_path, {})
    golden = _golden_with_candidate_file(tmp_path, relabel="allow-basic")

    verdict = run_gate(packs, golden, ACTIVE, CANDIDATE)

    assert verdict.changes == []
    assert not verdict.passed
    assert [(m.file, m.case_id) for m in verdict.own_file_mismatches] == [
        ("candidate.jsonl", "allow-basic")
    ]


def test_a_new_golden_file_is_compared_without_wiring(tmp_path: Path) -> None:
    packs = _candidate(tmp_path, {})
    golden = _golden_with_candidate_file(tmp_path)

    verdict = run_gate(packs, golden, ACTIVE, CANDIDATE)

    assert verdict.passed
    assert verdict.compared == DECISION_CASES + 36  # phase2's cases, once more


def test_a_case_that_cant_be_decided_fails_the_gate_and_is_named(tmp_path: Path) -> None:
    packs = _candidate(tmp_path, {})
    golden = _golden_with_candidate_file(tmp_path)
    path = golden / "candidate.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["request"] = {"amount": {"minor": -1, "currency": "INR"}}  # not a valid request
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    verdict = run_gate(packs, golden, ACTIVE, CANDIDATE)

    assert not verdict.passed
    assert len(verdict.errors) == 1
    assert verdict.errors[0].startswith(f"candidate.jsonl {rows[0]['id']}: ")


# --- The real repo ----------------------------------------------------------------------


def test_the_real_active_pack_against_itself_passes_with_no_changes() -> None:
    manifest = json.loads((PACKS / "manifest.json").read_text(encoding="utf-8"))
    active = PackPin(id="default", version=manifest["packs"]["default"]["active"])

    verdict = run_gate(PACKS, GOLDEN, active, active)

    assert verdict.passed
    assert (verdict.changes, verdict.errors) == ([], [])
    assert verdict.compared == DECISION_CASES


# --- The command ------------------------------------------------------------------------


def test_by_default_the_command_gates_the_active_pack_against_itself(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "default@1.5.0 -> default@1.5.0" in out
    assert "changed: 0" in out
    assert "PASS" in out


def test_the_command_exits_1_and_lists_each_change_on_a_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packs = _candidate(tmp_path, LOWER_INR_THRESHOLD)

    assert main(["--packs", str(packs), "--candidate", "default@1.6.0"]) == 1
    out = capsys.readouterr().out
    assert "phase2.jsonl allow-basic: allow low [] -> step_up high" in out
    assert "FAIL" in out


def test_the_command_names_each_high_risk_loosening(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packs = _candidate(tmp_path, {"currencies": {"INR": {"step_up_threshold": 2000000}}})

    assert main(["--packs", str(packs), "--candidate", "default@1.6.0"]) == 1
    out = capsys.readouterr().out
    assert "high-risk loosening: phase2.jsonl threshold-at" in out


@pytest.mark.parametrize("pin", ["1.6.0", "default@", "default@9.9.9", "other@1.0.0"])
def test_a_bad_or_unknown_pin_exits_2(pin: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--candidate", pin]) == 2
    assert pin in capsys.readouterr().err
