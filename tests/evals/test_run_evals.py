import json
import shutil
from pathlib import Path
from typing import get_args

import pytest

from core.domain.decisions import ReasonCode
from evals.run_evals import GOLDEN, load_cases, main, run_cases

PACKS = Path(__file__).resolve().parents[2] / "policy_packs"


def test_the_phase1_set_has_at_least_40_cases_covering_every_reason_code() -> None:
    cases = load_cases(GOLDEN / "phase1.jsonl")

    assert len(cases) >= 40
    assert len({c.id for c in cases}) == len(cases)
    covered = {code for c in cases for code in c.expected.reason_codes}
    assert covered == set(get_args(ReasonCode))
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
