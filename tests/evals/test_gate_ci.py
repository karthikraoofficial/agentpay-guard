"""CI's Release gate step: which pull requests run it, and on which pins. The rule is
tested on `git diff --name-status` output and two Manifests, not by running CI."""

import json
from pathlib import Path

import pytest

from core.policy.packs import Manifest
from evals.gate_ci import gates, run, triggered
from evals.run_evals import GOLDEN, PackPin
from tests.packs import PACKS, release_test_pack

PACK = "A\tpolicy_packs/default/1.6.0.yaml"
DECLARATIONS = "A\tpolicy_packs/default/1.6.0.declared-changes.yaml"
MANIFEST = "M\tpolicy_packs/manifest.json"
HASH = "sha256:" + "ab" * 32


def _manifest(active: str, *versions: str) -> Manifest:
    real = json.loads((PACKS / "manifest.json").read_text(encoding="utf-8"))
    entry = real["packs"]["default"]
    entry["versions"] |= {v: HASH for v in versions}
    entry["active"] = active
    return Manifest.model_validate(real)


def _pin(version: str) -> PackPin:
    return PackPin(id="default", version=version)


BASE = _manifest("1.5.0")


@pytest.mark.parametrize(
    "diff",
    [PACK, DECLARATIONS, MANIFEST, f"{PACK}\n{DECLARATIONS}\n{MANIFEST}"],
    ids=["new-pack", "new-declarations", "manifest", "all-three"],
)
def test_a_new_pack_a_new_declarations_file_or_a_manifest_change_triggers_the_gate(
    diff: str,
) -> None:
    assert triggered(diff)


@pytest.mark.parametrize(
    "diff",
    ["", "M\tREADME.md", "M\tevals/release_gate.py", "A\tpolicy_packs/schema.json"],
    ids=["nothing", "readme", "code", "pack-schema"],
)
def test_an_unrelated_change_doesnt_trigger_the_gate(diff: str) -> None:
    assert not triggered(diff)
    assert gates(diff, BASE, BASE) == []


def test_a_new_active_version_is_gated_against_the_base_branchs_active() -> None:
    head = _manifest("1.6.0", "1.6.0")

    assert gates(f"{PACK}\n{MANIFEST}", BASE, head) == [(_pin("1.5.0"), _pin("1.6.0"))]


def test_an_inactive_new_version_is_gated_against_the_active_one() -> None:
    head = _manifest("1.5.0", "1.6.0")

    assert gates(f"{PACK}\n{MANIFEST}", BASE, head) == [(_pin("1.5.0"), _pin("1.6.0"))]


def test_every_new_version_is_gated_against_the_base_branchs_active() -> None:
    head = _manifest("1.7.0", "1.6.0", "1.7.0")
    diff = f"{PACK}\nA\tpolicy_packs/default/1.7.0.yaml\n{MANIFEST}"

    assert gates(diff, BASE, head) == [
        (_pin("1.5.0"), _pin("1.6.0")),
        (_pin("1.5.0"), _pin("1.7.0")),
    ]


def test_moving_active_to_an_older_version_is_gated_too() -> None:
    head = _manifest("1.4.0")

    assert gates(MANIFEST, BASE, head) == [(_pin("1.5.0"), _pin("1.4.0"))]


# --- The step ---------------------------------------------------------------------------

LOWER_INR_THRESHOLD = {"currencies": {"INR": {"medium_from": 50000, "step_up_threshold": 100000}}}


def test_the_step_fails_on_an_undeclared_change_and_writes_the_job_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packs = release_test_pack(tmp_path / "packs", "1.6.0", LOWER_INR_THRESHOLD, based_on="1.5.0")
    summary = tmp_path / "summary.md"

    code = run(f"{PACK}\n{MANIFEST}", BASE, packs, GOLDEN, summary)

    assert code == 1
    text = summary.read_text(encoding="utf-8")
    assert "default@1.5.0 → default@1.6.0" in text
    assert "FAIL" in text
    for line in ("Compared", "Changed", "Declared", "Undeclared", "High-risk loosenings"):
        assert f"| {line} |" in text
    assert "Step-up rate" in text
    assert "| phase2.jsonl | allow-basic | allow low [] | step_up high" in text
    assert "FAIL" in capsys.readouterr().out


def test_the_step_passes_a_new_version_that_changes_nothing(tmp_path: Path) -> None:
    packs = release_test_pack(tmp_path / "packs", "1.6.0", {}, based_on="1.5.0", active=False)
    summary = tmp_path / "summary.md"

    assert run(f"{PACK}\n{MANIFEST}", BASE, packs, GOLDEN, summary) == 0
    text = summary.read_text(encoding="utf-8")
    assert "PASS" in text
    assert "| Changed | 0 |" in text


def test_the_step_skips_a_pull_request_that_doesnt_touch_packs(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"

    assert run("M\tREADME.md", BASE, PACKS, GOLDEN, summary) == 0
    assert "skipped" in summary.read_text(encoding="utf-8")


def test_the_step_exits_2_when_a_declarations_file_is_invalid(tmp_path: Path) -> None:
    packs = release_test_pack(tmp_path / "packs", "1.6.0", {}, based_on="1.5.0", active=False)
    (packs / "default" / "1.6.0.declared-changes.yaml").write_text("changes: 1\n")

    assert run(f"{PACK}\n{DECLARATIONS}\n{MANIFEST}", BASE, packs, GOLDEN, None) == 2
