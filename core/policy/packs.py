"""Policy packs: versioned, immutable rule parameters (ADR-0002).

A pack is a YAML file at `<root>/<pack id>/<version>.yaml` holding parameters and rule
order only, never logic. Its Pack hash is the SHA-256 of its validated content as
canonical JSON, so comments and formatting don't change it. `<root>/manifest.json`
records every released version's hash and the active version per pack id. Loading a
pack checks it against the Manifest and refuses one that no longer matches.
"""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from core.domain.canonical_json import canonical_json
from core.domain.mandates import Mcc
from core.domain.money import CurrencyCode

RuleId = Literal[
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
SemVer = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$", strict=True)]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CurrencyBands(_Strict):
    """Risk tier bands for one currency, in integer minor units. An amount below
    `medium_from` is low; from `step_up_threshold` up it is high and needs step_up."""

    medium_from: StrictInt = Field(gt=0)
    step_up_threshold: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "CurrencyBands":
        if self.medium_from >= self.step_up_threshold:
            raise ValueError("medium_from must be below step_up_threshold")
        return self


class RuleSetting(_Strict):
    rule_id: RuleId
    enabled: StrictBool


class PolicyPack(_Strict):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
    version: SemVer
    currencies: dict[CurrencyCode, CurrencyBands] = Field(min_length=1)
    high_risk_mccs: tuple[Mcc, ...]
    monthly_cap_near_ratio_bps: StrictInt = Field(gt=0, le=10_000)
    rules: tuple[RuleSetting, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_rules(self) -> "PolicyPack":
        ids = [r.rule_id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("each rule_id may appear only once")
        return self


class LoadedPack(BaseModel):
    model_config = ConfigDict(frozen=True)

    pack: PolicyPack
    hash: str


class PackError(Exception):
    """A pack is missing, invalid, or doesn't match its Manifest entry."""


def pack_hash(pack: PolicyPack) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(pack)).hexdigest()


class _ManifestEntry(_Strict):
    active: SemVer
    versions: dict[SemVer, Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]]


class Manifest(_Strict):
    packs: dict[str, _ManifestEntry]


def load_pack(root: Path, pack_id: str, version: str) -> LoadedPack:
    """Load one released version and check it against the Manifest."""
    entry = _manifest(root).packs.get(pack_id)
    expected = entry.versions.get(version) if entry else None
    if expected is None:
        raise PackError(f"{pack_id}@{version} is not in the Manifest")
    path = root / pack_id / f"{version}.yaml"
    try:
        pack = PolicyPack.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise PackError(f"{pack_id}@{version} could not be loaded: {exc}") from exc
    if (pack.id, pack.version) != (pack_id, version):
        raise PackError(f"{path} declares {pack.id}@{pack.version}")
    actual = pack_hash(pack)
    if actual != expected:
        raise PackError(
            f"{pack_id}@{version} hash {actual} does not match the Manifest's "
            f"{expected}; released packs are immutable, release a new version"
        )
    return LoadedPack(pack=pack, hash=actual)


def load_active_pack(root: Path, pack_id: str) -> LoadedPack:
    entry = _manifest(root).packs.get(pack_id)
    if entry is None:
        raise PackError(f"no pack {pack_id!r} in the Manifest")
    return load_pack(root, pack_id, entry.active)


def _manifest(root: Path) -> Manifest:
    path = root / "manifest.json"
    try:
        return Manifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise PackError(f"Manifest {path} could not be read: {exc}") from exc


def write_schema(root: Path) -> Path:
    """Write the JSON Schema for packs, generated from PolicyPack, for reviewers."""
    path = root / "schema.json"
    path.write_text(
        json.dumps(PolicyPack.model_json_schema(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def hash_file(path: Path) -> str:
    """The Pack hash of a pack file, for adding a new version to the Manifest."""
    return pack_hash(PolicyPack.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    import sys

    match sys.argv[1:]:
        case ["--write-schema"]:
            print(write_schema(Path("policy_packs")))
        case ["--hash", file]:
            print(hash_file(Path(file)))
        case _:
            raise SystemExit(
                "usage: python -m core.policy.packs --write-schema | --hash <pack.yaml>"
            )
