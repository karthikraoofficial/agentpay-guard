"""The Policy pack model: parameters and rule order, no logic and no I/O (ADR-0002).

Loading packs from disk and checking them against the Manifest is in core.policy.packs.
The policy engine imports only this module, so it never touches files.
"""

import hashlib
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
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


def pack_hash(pack: PolicyPack) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(pack)).hexdigest()
