"""The reconciliation sweep (#82): no Payment stays scheduled because a rail webhook was
lost. It asks the rail about every Payment still scheduled after a set age, across
Tenants, and applies the answer through the same transition a webhook uses, each under
its own Tenant. A rail error is logged and that Payment is tried again next time.

A Payment the rail has no charge for never reached it (the API stopped between saving
the Payment and calling the rail, and the Agent never retried). It is failed and its
Decision Voided rather than charged late: no money moved, and the Mandate may have
changed since.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from core.charges import apply_charge
from core.domain.decisions import decision_time
from core.ports import Clock, PaymentRail, RailCharge, TenantDirectory, UnitOfWork

log = logging.getLogger(__name__)

# The failure code of a Payment the rail has no charge for.
RAIL_CHARGE_MISSING = "RAIL_CHARGE_MISSING"


class SweepStore(UnitOfWork, TenantDirectory, Protocol):
    """Tenant transactions, plus the lookup of scheduled Payments across Tenants."""


@dataclass(frozen=True)
class SweepResult:
    checked: int
    errors: int


def sweep(store: SweepStore, rail: PaymentRail, clock: Clock, older_than: timedelta) -> SweepResult:
    now = decision_time(clock.now())
    due = store.scheduled_payments(created_before=now - older_than)
    errors = 0
    for tenant_id, payment_id in due:
        try:
            charge = rail.get_charge(tenant_id, payment_id)
            with store.transaction(tenant_id) as tx:
                payment = tx.get_payment(payment_id)
                if payment is None:
                    continue
                if charge is None:
                    log.warning("Payment %s has no Rail charge; failing it", payment_id)
                    charge = RailCharge(
                        payment_id=payment_id,
                        status="failed",
                        amount=payment.amount,
                        failure_code=RAIL_CHARGE_MISSING,
                    )
                apply_charge(tx, payment_id, charge, now)
        except Exception as exc:  # one Payment's trouble never stops the sweep
            errors += 1
            log.warning(
                "reconciling Payment %s for Tenant %s failed: %s",
                payment_id,
                tenant_id,
                type(exc).__name__,
            )
    return SweepResult(checked=len(due), errors=errors)
