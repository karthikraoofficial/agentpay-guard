"""The PaymentRails a deployment reaches (`AGENTPAY_RAILS`, ADR-0033), for the API and the
worker alike."""

from datetime import timedelta

from adapters.memory.rail import MockRail
from adapters.memory.settling_rail import SettlingMockRail
from adapters.razorpay.rail import RazorpayRail
from adapters.system_clock import SystemClock
from core.domain.rails import RailName
from core.ports import PaymentRail, UnitOfWork
from core.settings import Settings


def build_rails(settings: Settings, store: UnitOfWork | None = None) -> dict[RailName, PaymentRail]:
    """Each reachable rail by name. `store` lets the hosted demo's mock rail find debits
    the worker made (#280)."""
    return {name: build_rail(name, settings, store) for name in sorted(settings.reachable_rails)}


def build_rail(name: RailName, settings: Settings, store: UnitOfWork | None = None) -> PaymentRail:
    if name == "razorpay":
        return RazorpayRail.from_settings(settings)
    if settings.mock_notice_seconds is not None:
        # The hosted demo's card-like debit (#280): booked now, debited by the worker's
        # debit step once the notice period is over.
        return MockRail(
            notice=timedelta(seconds=settings.mock_notice_seconds),
            clock=SystemClock(),
            debits=None if store is None else SettlingMockRail(store).get_charge,
        )
    return MockRail()
