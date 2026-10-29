"""Test-only Policy packs: release a pack into a temporary packs directory.

Released packs are immutable (ADR-0002), so rules for an unreleased version are tested
against a copy of policy_packs/ with an extra version added to that copy's Manifest
and made active. The real policy_packs/ is never touched.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from core.policy.pack import PolicyPack, pack_hash

PACKS = Path(__file__).resolve().parents[1] / "policy_packs"


def _merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def release_test_pack(
    root: Path,
    version: str,
    changes: dict[str, Any],
    *,
    based_on: str = "1.0.0",
    active: bool = True,
) -> Path:
    """Copy policy_packs/ to `root` (once) and release `default@<version>` there: the
    `based_on` pack with `changes` merged in. Returns the packs directory to load from."""
    if not (root / "manifest.json").exists():
        shutil.copytree(PACKS, root, dirs_exist_ok=True)
    source = yaml.safe_load((root / "default" / f"{based_on}.yaml").read_text(encoding="utf-8"))
    pack = PolicyPack.model_validate(_merge(source, changes) | {"version": version})
    doc = pack.model_dump(mode="json")
    (root / "default" / f"{version}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["packs"]["default"]
    entry["versions"][version] = pack_hash(pack)
    if active:
        entry["active"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return root


def rules_plus(*rule_ids: str, based_on: str = "1.0.0") -> list[dict[str, Any]]:
    """The `based_on` pack's rule list with `rule_ids` appended, all enabled."""
    source = yaml.safe_load((PACKS / "default" / f"{based_on}.yaml").read_text(encoding="utf-8"))
    return [*source["rules"], *({"rule_id": r, "enabled": True} for r in rule_ids)]


VELOCITY = {"max_decisions": 5, "window_seconds": 600}
INTENT = {"verdict_ttl_seconds": 1800, "min_tier": "medium"}
PRICE_DRIFT = {"max_above_bps": 200}


def signals_pack(root: Path, version: str = "1.0.1") -> Path:
    """A test pack with every Signal built so far."""
    return release_test_pack(
        root,
        version,
        {
            "velocity": VELOCITY,
            "intent": INTENT,
            "price_drift": PRICE_DRIFT,
            "rules": rules_plus("velocity", "intent", "price_drift"),
        },
    )
