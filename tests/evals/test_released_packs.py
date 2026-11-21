"""The released-packs CI rule (ADR-0002, ADR-0028), tested on `git diff --name-status`
output rather than by running CI."""

import pytest

from evals.released_packs import violations


@pytest.mark.parametrize(
    "diff",
    [
        "",
        "M\tREADME.md",
        "M\tpolicy_packs/manifest.json",
        "A\tpolicy_packs/default/1.6.0.yaml",
        "A\tpolicy_packs/default/1.6.0.yaml\nA\tpolicy_packs/default/1.6.0.declared-changes.yaml",
    ],
    ids=["nothing", "other-file", "manifest", "new-pack", "new-pack-with-declarations"],
)
def test_adding_a_pack_with_its_declarations_is_allowed(diff: str) -> None:
    assert violations(diff) == []


@pytest.mark.parametrize(
    "diff",
    [
        "M\tpolicy_packs/default/1.5.0.yaml",
        "D\tpolicy_packs/default/1.5.0.yaml",
        "R100\tpolicy_packs/default/1.5.0.yaml\tpolicy_packs/default/1.5.1.yaml",
        "M\tpolicy_packs/default/1.6.0.declared-changes.yaml",
        "D\tpolicy_packs/default/1.6.0.declared-changes.yaml",
        "R100\tpolicy_packs/default/1.6.0.declared-changes.yaml\tpolicy_packs/default/x.yaml",
    ],
    ids=["edit", "delete", "rename", "edit-decl", "delete-decl", "rename-decl"],
)
def test_changing_a_released_pack_or_its_declarations_fails(diff: str) -> None:
    assert violations(diff) == [diff]


def test_declarations_can_only_be_added_together_with_their_pack() -> None:
    late = "A\tpolicy_packs/default/1.5.0.declared-changes.yaml"

    assert violations(late) == [late]
    assert violations(f"A\tpolicy_packs/default/1.6.0.yaml\n{late}") == [late]
