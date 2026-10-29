import json
import shutil
from pathlib import Path

import pytest

from core.policy.packs import PackError, PolicyPack, load_active_pack, load_pack

PACKS = Path(__file__).resolve().parents[2] / "policy_packs"


def test_the_active_default_pack_is_1_0_0_with_the_agreed_values() -> None:
    loaded = load_active_pack(PACKS, "default")
    pack = loaded.pack

    assert (pack.id, pack.version) == ("default", "1.0.0")
    assert pack.currencies["INR"].medium_from == 2_000_00
    assert pack.currencies["INR"].step_up_threshold == 5_000_00
    assert pack.currencies["USD"].medium_from == 25_00
    assert pack.currencies["USD"].step_up_threshold == 100_00
    assert set(pack.high_risk_mccs) == {"7995", "6051", "4829"}
    assert pack.monthly_cap_near_ratio_bps == 8000
    assert [r.rule_id for r in pack.rules] == [
        "mandate_revoked",
        "mandate_validity",
        "agent_allowed",
        "agent_disabled",
        "currency_match",
        "mcc_high_risk",
        "mcc_allowed",
        "per_txn_cap",
        "monthly_cap",
        "step_up_threshold",
        "monthly_cap_near",
    ]
    assert all(r.enabled for r in pack.rules)
    assert loaded.hash.startswith("sha256:") and len(loaded.hash) == len("sha256:") + 64


@pytest.fixture
def packs(tmp_path: Path) -> Path:
    """A writable copy of the real policy_packs directory."""
    shutil.copytree(PACKS, tmp_path, dirs_exist_ok=True)
    return tmp_path


def _pack_file(root: Path) -> Path:
    return root / "default" / "1.0.0.yaml"


def test_comments_and_formatting_do_not_change_the_pack_hash(packs: Path) -> None:
    original = load_active_pack(PACKS, "default").hash
    text = _pack_file(packs).read_text(encoding="utf-8")
    reformatted = "# an extra comment\n\n" + text.replace(
        "monthly_cap_near_ratio_bps: 8000", "monthly_cap_near_ratio_bps:    8000   # same value"
    )
    _pack_file(packs).write_text(reformatted, encoding="utf-8")

    assert load_active_pack(packs, "default").hash == original


def test_changing_a_value_in_a_released_pack_is_refused(packs: Path) -> None:
    text = _pack_file(packs).read_text(encoding="utf-8")
    _pack_file(packs).write_text(text.replace("500000", "500100"), encoding="utf-8")

    with pytest.raises(PackError, match="does not match the Manifest"):
        load_active_pack(packs, "default")


def test_a_version_missing_from_the_manifest_is_refused() -> None:
    with pytest.raises(PackError, match="not in the Manifest"):
        load_pack(PACKS, "default", "9.9.9")


@pytest.mark.parametrize(
    ("find", "replace"),
    [
        ("monthly_cap_near_ratio_bps: 8000", "monthly_cap_near_ratio_bps: 80.0"),
        ("high_risk_mccs:", "surprise: true\nhigh_risk_mccs:"),
        ('"4829"', "4829"),
    ],
    ids=["float", "unknown-field", "unquoted-mcc"],
)
def test_an_invalid_pack_is_refused(packs: Path, find: str, replace: str) -> None:
    text = _pack_file(packs).read_text(encoding="utf-8")
    assert find in text
    _pack_file(packs).write_text(text.replace(find, replace), encoding="utf-8")

    with pytest.raises(PackError, match="could not be loaded"):
        load_active_pack(packs, "default")


def test_the_committed_json_schema_matches_the_pack_model() -> None:
    committed = json.loads((PACKS / "schema.json").read_text(encoding="utf-8"))

    assert committed == PolicyPack.model_json_schema(), (
        "policy_packs/schema.json is stale; regenerate it with "
        "`uv run python -m core.policy.packs --write-schema`"
    )


def test_a_pack_that_enables_velocity_without_its_parameters_is_refused() -> None:
    doc = load_active_pack(PACKS, "default").pack.model_dump(mode="json", exclude_none=True)
    doc["rules"] = [*doc["rules"], {"rule_id": "velocity", "enabled": True}]

    with pytest.raises(ValueError, match="velocity rule needs a velocity section"):
        PolicyPack.model_validate(doc)
    assert PolicyPack.model_validate(
        doc | {"velocity": {"max_decisions": 5, "window_seconds": 600}}
    )
