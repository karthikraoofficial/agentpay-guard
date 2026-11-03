"""The Policy pack model: parameters and rule order, no logic and no I/O (ADR-0002).

Loading packs from disk and checking them against the Manifest is in core.policy.packs.
The policy engine imports only this module, so it never touches files.
"""

import hashlib
from datetime import timedelta
from typing import Annotated, Literal, get_args

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
from core.domain.disputes import DisputeReason, EvidenceFact
from core.domain.mandates import Mcc
from core.domain.money import CurrencyCode

# The consent TTL of packs released before `consent_ttl_seconds` existed.
LEGACY_CONSENT_TTL_SECONDS = 600
# The Payment window of packs released before `payment_window_seconds` existed.
LEGACY_PAYMENT_WINDOW_SECONDS = 900

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


class NetworkCodes(_Strict):
    """The card network reason codes a Dispute reason is filed under."""

    visa: Annotated[str, StringConstraints(pattern=r"^\d{2}(\.\d+)+$", strict=True)]
    mastercard: Annotated[str, StringConstraints(pattern=r"^\d{4}$", strict=True)]


class DisputeReasonRules(_Strict):
    """How one Dispute reason is filed and weighed. Evidence strength is weak if any
    `weak_when` fact holds, strong if every `strong_when` fact holds, and mixed otherwise.
    `strong_when` is also the evidence the reason needs: what is missing from it is shown."""

    network_codes: NetworkCodes
    strong_when: tuple[EvidenceFact, ...] = Field(min_length=1)
    weak_when: tuple[EvidenceFact, ...] = ()


class DisputeRules(_Strict):
    """Network codes and Evidence strength for Disputes, one entry per Dispute reason."""

    # How close in time another captured Payment by the same Payer, to the same merchant
    # for the same amount, must be to count as a duplicate.
    duplicate_window_seconds: StrictInt = Field(gt=0)
    reasons: dict[DisputeReason, DisputeReasonRules]

    @model_validator(mode="after")
    def _every_reason(self) -> "DisputeRules":
        missing = set(get_args(DisputeReason)) - set(self.reasons)
        if missing:
            raise ValueError(f"every Dispute reason needs rules; missing {sorted(missing)}")
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
    # Signal parameters (ADR-0006). Optional, so packs released before a Signal existed
    # still validate, and left out of the Pack hash when unset.
    velocity: VelocityParams | None = None
    merchant_new: MerchantNewParams | None = None
    intent: IntentParams | None = None
    price_drift: PriceDriftParams | None = None
    # How long the Payer has to answer a Consent request. Optional for the same reason;
    # unset means LEGACY_CONSENT_TTL_SECONDS.
    consent_ttl_seconds: StrictInt | None = Field(default=None, gt=0)
    # How long an allow, or an approved step-up, can be paid. No rule reads it: the API
    # derives `payable_until` from it. Unset means LEGACY_PAYMENT_WINDOW_SECONDS.
    payment_window_seconds: StrictInt | None = Field(default=None, gt=0)
    # Network codes and Evidence strength for Disputes (#106). Optional: a pack without it
    # builds Evidence packs with no codes or strength.
    disputes: DisputeRules | None = None

    @property
    def consent_ttl(self) -> timedelta:
        return timedelta(seconds=self.consent_ttl_seconds or LEGACY_CONSENT_TTL_SECONDS)

    @property
    def payment_window(self) -> timedelta:
        return timedelta(seconds=self.payment_window_seconds or LEGACY_PAYMENT_WINDOW_SECONDS)

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
