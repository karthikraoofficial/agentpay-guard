"""The Eval report: `run_evals` publishes deterministic numbers and fails when they're stale."""

import json
import shutil
from pathlib import Path

import pytest

from evals.run_evals import GOLDEN, main

README = """# Project

Intro stays.

<!-- eval-report:start -->
old table
<!-- eval-report:end -->

Outro stays.
"""

# Hand-labelled from default@1.0.0: allow, step_up (threshold), block (revoked),
# step_up (at the per-transaction cap). Three carry a scenario tag, one doesn't.
CASES = [
    {
        "id": "allow-basic",
        "description": "Groceries inside every limit",
        "scenario": "clean_allow",
        "expected": {"outcome": "allow", "reason_codes": []},
    },
    {
        "id": "threshold-at",
        "description": "INR 5,000.00 steps up",
        "scenario": "over_cap",
        "request": {"amount": {"minor": 500000, "currency": "INR"}},
        "expected": {"outcome": "step_up", "reason_codes": ["AMOUNT_ABOVE_STEP_UP_THRESHOLD"]},
    },
    {
        "id": "revoked",
        "description": "Mandate revoked by the Payer",
        "scenario": "expired_mandate",
        "mandate": {"revoked_by": "payer", "revoked_at": "2026-11-10T08:00:00Z"},
        "expected": {"outcome": "block", "reason_codes": ["MANDATE_REVOKED"]},
    },
    {
        "id": "per-txn-at-cap",
        "description": "Exactly the per-transaction cap is not a breach",
        "request": {"amount": {"minor": 1000000, "currency": "INR"}},
        "expected": {"outcome": "step_up", "reason_codes": ["AMOUNT_ABOVE_STEP_UP_THRESHOLD"]},
    },
]


def _golden(tmp_path: Path, cases: list[dict[str, object]] = CASES) -> Path:
    golden = tmp_path / "golden"
    golden.mkdir()
    shutil.copy(GOLDEN / "phase1_base.json", golden / "mini_base.json")
    (golden / "mini.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases), "utf-8")
    shutil.copy(GOLDEN / "narrative_checks_base.json", golden / "narrative_checks_base.json")
    shutil.copy(GOLDEN / "narrative_checks.jsonl", golden / "narrative_checks.jsonl")
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    return golden


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--golden",
        str(tmp_path / "golden"),
        "--report",
        str(tmp_path / "report.json"),
        "--readme",
        str(tmp_path / "README.md"),
        *extra,
    ]


def test_the_report_holds_counts_rates_and_scenarios(tmp_path: Path) -> None:
    _golden(tmp_path)

    assert main(_args(tmp_path, "--write-report")) == 0

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["decision_cases"] == 4
    assert (report["false_allow"], report["false_block"], report["wrong_reason"]) == (0, 0, 0)
    assert report["mismatches"] == 0
    assert report["step_up_rate"] == 0.5
    mini = next(f for f in report["files"] if f["file"] == "mini.jsonl")
    assert mini["pack"] == "default@1.0.0"
    assert (mini["cases"], mini["allow"], mini["step_up"], mini["block"]) == (4, 1, 2, 1)
    checks = next(f for f in report["files"] if f["file"] == "narrative_checks.jsonl")
    assert (checks["kind"], checks["cases"], checks["mismatches"]) == ("narrative", 18, 0)
    assert report["scenarios"] == {
        "clean_allow": {"cases": 1, "allow": 1, "step_up": 0, "block": 0},
        "expired_mandate": {"cases": 1, "allow": 0, "step_up": 0, "block": 1},
        "over_cap": {"cases": 1, "allow": 0, "step_up": 1, "block": 0},
    }


def test_a_fresh_report_passes_and_a_stale_one_fails_with_the_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _golden(tmp_path)
    assert main(_args(tmp_path, "--write-report")) == 0
    assert main(_args(tmp_path)) == 0

    more = [*CASES, {**CASES[0], "id": "allow-again", "scenario": "clean_allow"}]
    shutil.rmtree(tmp_path / "golden")
    _golden(tmp_path, more)
    capsys.readouterr()

    assert main(_args(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "report.json" in out
    assert "--write-report" in out


def test_a_readme_table_edited_by_hand_is_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _golden(tmp_path)
    assert main(_args(tmp_path, "--write-report")) == 0
    readme = tmp_path / "README.md"
    readme.write_text(readme.read_text("utf-8").replace("| 4 |", "| 5 |"), "utf-8")

    assert main(_args(tmp_path)) == 1
    assert "eval table" in capsys.readouterr().out


def test_the_readme_table_replaces_only_the_marked_block(tmp_path: Path) -> None:
    _golden(tmp_path)

    assert main(_args(tmp_path, "--write-report")) == 0

    readme = (tmp_path / "README.md").read_text("utf-8")
    assert readme.startswith("# Project\n\nIntro stays.\n\n<!-- eval-report:start -->\n")
    assert readme.endswith("<!-- eval-report:end -->\n\nOutro stays.\n")
    assert "old table" not in readme
    assert "**4 labelled Decision cases.**" in readme
    assert "Step-up rate 50.0%" in readme
    assert "| `mini.jsonl` | default@1.0.0 | 4 | 1 | 2 | 1 | 0 | 0 | 0 | 0 |" in readme
    assert "| over_cap | 1 | 0 | 1 | 0 |" in readme


def test_a_right_outcome_with_a_wrong_reason_code_is_counted_and_fails(tmp_path: Path) -> None:
    wrong = {**CASES[2], "expected": {"outcome": "block", "reason_codes": ["MANDATE_EXPIRED"]}}
    _golden(tmp_path, [CASES[0], CASES[1], wrong, CASES[3]])

    assert main(_args(tmp_path, "--write-report")) == 1

    report = json.loads((tmp_path / "report.json").read_text("utf-8"))
    assert (report["false_allow"], report["false_block"], report["wrong_reason"]) == (0, 0, 1)
    assert report["mismatches"] == 1


def test_the_run_prints_engine_p95_and_the_step_up_rate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _golden(tmp_path)

    main(_args(tmp_path, "--write-report"))

    out = capsys.readouterr().out
    assert "engine p95: " in out
    assert "over 4 decide() calls" in out
    assert "step-up rate: 50.0%" in out
    # Latency depends on the machine, so the committed report never holds it.
    report = json.loads((tmp_path / "report.json").read_text("utf-8"))
    assert set(report) == {
        "about",
        "decision_cases",
        "false_allow",
        "false_block",
        "wrong_reason",
        "mismatches",
        "step_up_rate",
        "files",
        "scenarios",
    }


def test_writing_the_report_from_one_file_is_refused(tmp_path: Path) -> None:
    golden = _golden(tmp_path)

    with pytest.raises(SystemExit):
        main(["--cases", str(golden / "mini.jsonl"), "--write-report"])
