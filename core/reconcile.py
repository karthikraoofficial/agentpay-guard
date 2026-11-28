"""The reconciliation sweep (#82): no Payment stays scheduled because a rail webhook was
lost. It asks the rail about every Payment still scheduled after a set age, across
Tenants, and applies the answer through the same transition a webhook uses, each under
its own Tenant. A rail error is logged and that Payment is tried again next time.

A Payment the rail has no charge for never reached it (the API stopped between saving
the Payment and calling the rail, and the Agent never retried). It is failed and its
Decision Voided rather than charged late: no money moved, and the Mandate may have
changed since (ADR-0016). Only a Payment the rail never answered for is treated so; one
the rail answered for (with a rail payment id or a debit time) but has no charge for is a
lookup fault, retried. A Payment in its Pre-debit notice period is left alone.

Pending Refunds are swept the same way (#209), by Refund id. One the rail has no refund
for and never answered for never reached it: it is failed, so the Tenant can try again,
and no money moved. One the rail answered for but can't find is a lookup fault, retried.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from core.charges import apply_charge
from core.domain.decisions import decision_time
from core.domain.rails import RailName
from core.ports import (
    Clock,
    PaymentRail,
    RailCharge,
    RailRefund,
    TenantDirectory,
    TenantId,
    UnitOfWork,
)
from core.refunds import record_refund

log = logging.getLogger(__name__)

# The failure code of a Payment the rail has no charge for.
RAIL_CHARGE_MISSING = "RAIL_CHARGE_MISSING"
# The failure code of a Refund the rail has no refund for.
RAIL_REFUND_MISSING = "RAIL_REFUND_MISSING"


class SweepStore(UnitOfWork, TenantDirectory, Protocol):
    """Tenant transactions, plus the lookup of scheduled Payments across Tenants."""


@dataclass(frozen=True)
class SweepResult:
    checked: int
    errors: int
    # Still scheduled or pending past the sweep age once the pass is done (#228).
    unsettled_payments: int = 0
    unsettled_refunds: int = 0

    def __add__(self, other: "SweepResult") -> "SweepResult":
        """Two rails' passes as one (ADR-0033), so the metrics count every rail."""
        return SweepResult(
            checked=self.checked + other.checked,
            errors=self.errors + other.errors,
            unsettled_payments=self.unsettled_payments + other.unsettled_payments,
            unsettled_refunds=self.unsettled_refunds + other.unsettled_refunds,
        )


def sweep(
    store: SweepStore,
    rail: PaymentRail,
    clock: Clock,
    older_than: timedelta,
    on_rail: RailName | None = None,
    demo_tenant: TenantId | None = None,
) -> SweepResult:
    """Reconcile the Payments and Refunds on `on_rail` (every rail if None) with `rail`,
    which must be the rail they are on (ADR-0033). `demo_tenant` is
    `AGENTPAY_DEMO_CONTROLS_TENANT_ID`: its debited mock Payments that a settle-now Demo
    control names are settled now, however young (#287, ADR-0032)."""
    now = decision_time(clock.now())
    # Never sooner than the rail can see a new charge, or it would fail one it just made.
    due = store.scheduled_payments(now - max(older_than, rail.lookup_lag), now, on_rail)
    if demo_tenant is not None and on_rail in (None, "mock"):
        listed = set(due)
        named = store.settled_now_by_control(demo_tenant)
        due += [(demo_tenant, p) for p in named if (demo_tenant, p) not in listed]
    errors = 0
    for tenant_id, payment_id in due:
        try:
            charge = rail.get_charge(tenant_id, payment_id)
            with store.transaction(tenant_id) as tx:
                payment = tx.get_payment(payment_id)
                if payment is None:
                    continue
                if charge is None and payment.rail_answered:
                    # The rail answered for it once, so this is a lookup fault, not a
                    # charge that never reached the rail: try again next sweep.
                    raise LookupError(f"the rail lost track of Payment {payment_id}")
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
                exc_info=True,
            )
    created_before = now - max(older_than, rail.lookup_lag)
    refunds = _sweep_refunds(store, rail, created_before, now, on_rail)
    return SweepResult(
        checked=len(due) + refunds.checked,
        errors=errors + refunds.errors,
        unsettled_payments=len(store.scheduled_payments(created_before, now, on_rail)),
        unsettled_refunds=len(store.pending_refunds(created_before, on_rail)),
    )


def _sweep_refunds(
    store: SweepStore,
    rail: PaymentRail,
    created_before: datetime,
    now: datetime,
    on_rail: RailName | None,
) -> SweepResult:
    due = store.pending_refunds(created_before, on_rail)
    errors = 0
    for tenant_id, payment_id, refund_id in due:
        try:
            answer = rail.get_refund(tenant_id, payment_id, refund_id)
            with store.transaction(tenant_id) as tx:
                refund = tx.lock_refund(refund_id)
                if refund is None:
                    continue
                if answer is None and refund.rail_refund_id is not None:
                    raise LookupError(f"the rail lost track of Refund {refund_id}")
                if answer is None:
                    log.warning("Refund %s has no rail refund; failing it", refund_id)
                    answer = RailRefund(
                        refund_id=refund_id,
                        status="failed",
                        amount=refund.amount,
                        failure_code=RAIL_REFUND_MISSING,
                    )
                record_refund(tx, refund_id, answer, now)
        except Exception as exc:  # one Refund's trouble never stops the sweep
            errors += 1
            log.warning(
                "reconciling Refund %s for Tenant %s failed: %s",
                refund_id,
                tenant_id,
                type(exc).__name__,
                exc_info=True,
            )
    return SweepResult(checked=len(due), errors=errors)
