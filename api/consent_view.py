"""What the consent screen shows a Payer: the purchase, and why it was flagged, in plain
words (#62). Built only from facts AgentPay Guard worked out or recorded itself, never
from Agent reasoning or the Payer instruction, so a manipulated Agent can't talk the
Payer into approving. The Payer may see their own thresholds and caps here: unlike an
Agent's response, this view needs a Consent link, which an Agent never holds."""

from datetime import datetime

from core.domain.audit import DecisionInputs
from core.domain.decisions import ReasonCode
from core.domain.mandates import Mandate
from core.domain.money import Money
from core.domain.parties import Agent
from core.policy.pack import PolicyPack


def money_text(money: Money) -> str:
    """INR and USD both have two minor digits: 600000 INR is 'INR 6,000.00'."""
    return f"{money.currency} {money.minor // 100:,}.{money.minor % 100:02d}"


def mandate_inactive(mandate: Mandate | None, agent: Agent | None, now: datetime) -> bool:
    """Whether an approval would now be blocked for a reason the Payer can see coming:
    the Mandate is no longer active, or the Agent was disabled (worked out when read)."""
    if mandate is None or mandate.status(now) != "active":
        return True
    return agent is None or agent.status == "disabled"


def reason_text(code: ReasonCode, asked: DecisionInputs, pack: PolicyPack) -> str:
    request, signals = asked.request, asked.signals
    amount = request.amount
    if code == "AMOUNT_ABOVE_STEP_UP_THRESHOLD":
        bands = pack.currencies.get(amount.currency)
        if bands is not None:
            threshold = Money(minor=bands.step_up_threshold, currency=amount.currency)
            return (
                f"This payment is {money_text(amount)}. Payments of {money_text(threshold)} "
                "or more always need your approval."
            )
    if code == "MONTHLY_CAP_NEAR":
        after = Money(minor=asked.spent_this_month.minor + amount.minor, currency=amount.currency)
        cap = asked.mandate.monthly_cap
        return (
            f"With this payment you would have spent {money_text(after)} of your "
            f"{money_text(cap)} monthly limit."
        )
    if code == "VELOCITY_HIGH" and pack.velocity is not None:
        minutes = pack.velocity.window_seconds // 60
        return (
            f"The agent has asked to pay {signals.velocity_count} times in the last "
            f"{minutes} minutes."
        )
    if code == "MERCHANT_NEW":
        return f"You haven't paid {request.merchant.name} before."
    if code == "INTENT_MISSING":
        return "The agent didn't check this cart against what you asked it to buy."
    if code == "INTENT_MISMATCH":
        return "This cart doesn't match what you asked the agent to buy."
    if code == "INTENT_UNKNOWN":
        return "We couldn't confirm that this cart matches what you asked the agent to buy."
    if code == "PRICE_DRIFT" and signals.intent is not None:
        quoted = signals.intent.quoted_total
        return (
            f"The price is {money_text(amount)}, more than the {money_text(quoted)} the agent "
            "was quoted."
        )
    return "This payment needs your approval."
