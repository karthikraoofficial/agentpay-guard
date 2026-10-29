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
    "velocity",
    "merchant_new",
    "intent",
    "price_drift",
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


class VelocityParams(_Strict):
    """Step up once `max_decisions` earlier Decisions on the Mandate, of any Outcome,
    were made less than `window_seconds` before the Decision time (or at it)."""

    max_decisions: StrictInt = Field(gt=0)
    window_seconds: StrictInt = Field(gt=0)


class IntentParams(_Strict):
    """How long an Intent verdict can be cited after the check, and the lowest Risk tier
    that needs a usable match."""

    verdict_ttl_seconds: StrictInt = Field(gt=0)
    min_tier: Literal["low", "medium", "high"]


class MerchantNewParams(_Strict):
    """The lowest Risk tier at which a first payment to a merchant needs approval."""

    min_tier: Literal["low", "medium", "high"]


class PriceDriftParams(_Strict):
    """Step up when the amount is more than `max_above_bps` above the Quoted total."""

    max_above_bps: StrictInt = Field(ge=0, le=10_000)


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
    # Signal parameters (ADR-0006). Optional, so packs released before a Signal existed
    # still validate, and left out of the Pack hash when unset.
    velocity: VelocityParams | None = None
    merchant_new: MerchantNewParams | None = None
    intent: IntentParams | None = None
    price_drift: PriceDriftParams | None = None

    @model_validator(mode="after")
    def _unique_rules(self) -> "PolicyPack":
        ids = [r.rule_id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("each rule_id may appear only once")
        for rule_id, section in (
            ("velocity", self.velocity),
            ("merchant_new", self.merchant_new),
            ("intent", self.intent),
            ("price_drift", self.price_drift),
        ):
            if rule_id in ids and section is None:
                raise ValueError(f"the {rule_id} rule needs a {rule_id} section")
        return self


class LoadedPack(BaseModel):
    model_config = ConfigDict(frozen=True)

    pack: PolicyPack
    hash: str


def pack_hash(pack: PolicyPack) -> str:
    """SHA-256 of the pack's canonical JSON. Optional sections a pack doesn't set are
    left out, so adding one to the model never changes a released pack's hash."""
    content = pack.model_dump(mode="python", exclude_none=True)
    return "sha256:" + hashlib.sha256(canonical_json(content)).hexdigest()
