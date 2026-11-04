import json
import shutil
from pathlib import Path
from typing import get_args

import pytest

from core.domain.decisions import ReasonCode
from core.domain.disputes import DisputeReason
from core.domain.evidence import CheckCode
from evals.disputes import (
    load_dispute_cases,
    load_narrative_cases,
    run_dispute_cases,
    run_narrative_cases,
)
from evals.run_evals import GOLDEN, PackPin, load_cases, main, run_cases
from tests.packs import release_test_pack

PACKS = Path(__file__).resolve().parents[2] / "policy_packs"
# Codes default@1.0.0 can never produce; the Phase 2 golden set covers them.
SIGNAL_CODES = {
    "VELOCITY_HIGH",
    "MERCHANT_NEW",
    "INTENT_MISSING",
    "INTENT_MISMATCH",
    "INTENT_UNKNOWN",
    "PRICE_DRIFT",
}


def test_the_phase1_set_has_at_least_40_cases_covering_every_phase_1_reason_code() -> None:
    cases = load_cases(GOLDEN / "phase1.jsonl")

    assert len(cases) >= 40
    assert len({c.id for c in cases}) == len(cases)
    covered = {code for c in cases for code in c.expected.reason_codes}
    assert covered == set(get_args(ReasonCode)) - SIGNAL_CODES
    assert {c.expected.outcome for c in cases} == {"allow", "step_up", "block"}


def test_the_engine_matches_every_label(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "false-allow: 0" in out
    assert "false-block: 0" in out


def _with_label(tmp_path: Path, case_id: str, outcome: str, codes: list[str]) -> Path:
    shutil.copytree(GOLDEN, tmp_path, dirs_exist_ok=True)
    path = tmp_path / "phase1.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["id"] == case_id:
            row["expected"] = {"outcome": outcome, "reason_codes": codes}
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_a_wrong_label_is_reported_as_a_false_allow(tmp_path: Path) -> None:
    path = _with_label(tmp_path, "allow-basic", "block", ["MCC_NOT_ALLOWED"])

    report = run_cases(load_cases(path), PACKS)

    assert (report.false_allow, report.false_block) == (1, 0)
    assert [m.case_id for m in report.mismatches] == ["allow-basic"]


def test_a_wrong_label_is_reported_as_a_false_block(tmp_path: Path) -> None:
    path = _with_label(tmp_path, "revoked", "allow", [])

    report = run_cases(load_cases(path), PACKS)

    assert (report.false_allow, report.false_block) == (0, 1)


def test_a_wrong_reason_code_alone_still_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _with_label(tmp_path, "per-txn-over", "block", ["PER_TXN_CAP_EXCEEDED"])

    assert main(["--cases", str(path)]) == 1
    out = capsys.readouterr().out
    assert "per-txn-over" in out
    assert "AMOUNT_ABOVE_STEP_UP_THRESHOLD" in out


def test_the_phase1_set_is_pinned_to_default_1_0_0() -> None:
    cases = load_cases(GOLDEN / "phase1.jsonl")

    assert {(c.pack.id, c.pack.version) for c in cases} == {("default", "1.0.0")}


def test_each_golden_file_decides_with_the_pack_its_base_names(tmp_path: Path) -> None:
    # A pack whose INR Step-up threshold is INR 1,000, below the base request's INR 1,250.
    packs = release_test_pack(
        tmp_path / "packs",
        "1.0.1",
        {"currencies": {"INR": {"medium_from": 50000, "step_up_threshold": 100000}}},
        active=True,
    )
    golden = tmp_path / "golden"
    shutil.copytree(GOLDEN, golden)
    base_path = golden / "phase1_base.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))
    base["pack"] = {"id": "default", "version": "1.0.1"}
    base_path.write_text(json.dumps(base), encoding="utf-8")

    report = run_cases(load_cases(golden / "phase1.jsonl"), packs)

    assert "allow-basic" in {m.case_id for m in report.mismatches}
    # The real Phase 1 file stays on 1.0.0 even though 1.0.1 is active here.
    pinned = run_cases(load_cases(GOLDEN / "phase1.jsonl"), packs)
    assert pinned.mismatches == []


def test_the_phase2_set_is_pinned_to_1_1_0_and_covers_every_signal_code() -> None:
    cases = load_cases(GOLDEN / "phase2.jsonl")

    assert {(c.pack.id, c.pack.version) for c in cases} == {("default", "1.1.0")}
    assert len({c.id for c in cases}) == len(cases)
    covered = {code for c in cases for code in c.expected.reason_codes}
    assert covered >= SIGNAL_CODES
    assert {c.expected.outcome for c in cases} == {"allow", "step_up", "block"}


def test_by_default_every_golden_file_runs_against_its_own_pack(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "phase1.jsonl" in out and "phase2.jsonl" in out


def test_the_approval_set_covers_both_resolutions_and_every_wait_change() -> None:
    cases = load_cases(GOLDEN / "approval.jsonl")

    assert {c.stage for c in cases} == {"approval"}
    assert {c.expected.resolution for c in cases} == {"approved", "blocked"}
    primary = {c.expected.reason_codes[0] for c in cases if c.expected.resolution == "blocked"}
    assert primary == {
        "MANDATE_REVOKED",
        "MANDATE_EXPIRED",
        "AGENT_DISABLED",
        "MONTHLY_CAP_EXCEEDED",
    }


def test_an_approval_labelled_blocked_that_goes_through_is_a_false_allow(tmp_path: Path) -> None:
    shutil.copytree(GOLDEN, tmp_path, dirs_exist_ok=True)
    path = tmp_path / "approval.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["id"] == "approve-unchanged":
            row["expected"]["resolution"] = "blocked"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    report = run_cases(load_cases(path), PACKS)

    assert (report.false_allow, report.false_block) == (1, 0)
    assert [m.case_id for m in report.mismatches] == ["approve-unchanged"]


def test_the_approval_set_is_pinned_to_1_3_0() -> None:
    cases = load_cases(GOLDEN / "approval.jsonl")

    assert {(c.pack.id, c.pack.version) for c in cases} == {("default", "1.3.0")}


@pytest.mark.parametrize("version", ["1.3.0", "1.4.0"])
@pytest.mark.parametrize("name", ["phase2", "approval"])
def test_later_packs_decide_every_signal_era_case_as_its_own_pack_does(
    name: str, version: str
) -> None:
    # 1.3.0 only adds the Payment window and 1.4.0 the Dispute rules, which no rule reads.
    # phase1 is left out: it is labelled for 1.0.0, before the Signals, so later packs are
    # meant to decide some of it apart.
    cases = load_cases(GOLDEN / f"{name}.jsonl")
    later = [c.model_copy(update={"pack": PackPin(id="default", version=version)}) for c in cases]

    assert run_cases(cases, PACKS).mismatches == []
    timing = {"engine_ms"}  # wall time differs run to run
    assert run_cases(later, PACKS).model_dump(exclude=timing) == run_cases(cases, PACKS).model_dump(
        exclude=timing
    )


# --- Disputes -------------------------------------------------------------------------


def test_the_dispute_set_covers_every_reason_and_strength_on_1_4_0() -> None:
    cases = load_dispute_cases(GOLDEN / "disputes.jsonl")

    assert len({c.id for c in cases}) == len(cases)
    assert {(c.pack.id, c.pack.version) for c in cases} == {("default", "1.4.0")}
    assert {c.reason for c in cases} == set(get_args(DisputeReason))
    assert {c.expected.strength for c in cases} == {"strong", "mixed", "weak"}
    assert run_dispute_cases(cases, PACKS).mismatches == []


def test_a_wrong_dispute_label_fails_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copy(GOLDEN / "disputes_base.json", tmp_path / "disputes_base.json")
    lines = (GOLDEN / "disputes.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["expected"]["strength"] = "weak"
    (tmp_path / "disputes.jsonl").write_text(json.dumps(first), encoding="utf-8")

    assert main(["--cases", str(tmp_path / "disputes.jsonl")]) == 1
    assert "mismatches: 1" in capsys.readouterr().out


def test_the_narrative_set_covers_every_refusal_and_acceptance() -> None:
    cases = load_narrative_cases(GOLDEN / "narrative_checks.jsonl")

    assert len({c.id for c in cases}) == len(cases)
    assert {code for c in cases for code in c.expected.codes} == set(get_args(CheckCode))
    assert {c.expected.accepted for c in cases} == {True, False}
    assert run_narrative_cases(cases).mismatches == []


def test_a_wrong_narrative_label_fails_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copy(GOLDEN / "narrative_checks_base.json", tmp_path / "narrative_checks_base.json")
    lines = (GOLDEN / "narrative_checks.jsonl").read_text(encoding="utf-8").splitlines()
    good = json.loads(lines[0])
    good["expected"] = {"accepted": False, "codes": ["uncited"]}
    (tmp_path / "narrative_checks.jsonl").write_text(json.dumps(good), encoding="utf-8")

    assert main(["--cases", str(tmp_path / "narrative_checks.jsonl")]) == 1
    assert "mismatches: 1" in capsys.readouterr().out
