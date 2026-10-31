"""The PaymentRail that `AGENTPAY_RAIL` names, for the API and the worker alike."""

from adapters.memory.rail import MockRail
from adapters.razorpay.rail import RazorpayRail
from core.ports import PaymentRail
from core.settings import Settings


def build_rail(settings: Settings) -> PaymentRail:
    if settings.rail == "razorpay":
        return RazorpayRail.from_settings(settings)
    return MockRail()
