"""The debit step: debit the card for each Payment whose Pre-debit notice period has ended
(ADR-0017). The worker runs it before each reconciliation sweep, in one process: the rail
never debits one Payment twice when asked one after another.

Just before the debit it checks again that the Mandate is active and the Agent enabled,
as a payment does. If either no longer holds, the Payment fails with that Reason code and
its Decision is Voided, so a Revocation during the notice period stops the debit. The one
exception is a debit the rail already made whose answer was lost: the rail is asked first,
and a Payment whose money moved is never failed. The rail call is outside any
transaction; its answer goes through `apply_charge`, the same transition a rail webhook
uses. A rail error is logged and the Payment tried again next time.
"""

import logging
from dataclasses import dataclass

from core.charges import apply_charge
from core.domain.decisions import decision_time
from core.domain.payments import Payment, PaymentId, PaymentRefusal, debit_refusal
from core.domain.rails import RailToken
from core.ports import Clock, IdempotencyKey, PaymentRail, RailCharge, TenantId
from core.reconcile import SweepStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DebitResult:
    checked: int
    errors: int


@dataclass(frozen=True)
class _Due:
    payment: Payment
    rail_token: RailToken
    stop: PaymentRefusal | None  # why the debit may no longer be made, if it may not


def run_debit_step(store: SweepStore, rail: PaymentRail, clock: Clock) -> DebitResult:
    due = store.due_debits(decision_time(clock.now()))
    errors = 0
    for tenant_id, payment_id in due:
        try:
            _debit_one(store, rail, clock, tenant_id, payment_id)
        except Exception as exc:  # one Payment's trouble never stops the step
            errors += 1
            log.error("debit of Payment %s failed: %r", payment_id, exc, exc_info=exc)
    return DebitResult(checked=len(due), errors=errors)


def _debit_one(
    store: SweepStore, rail: PaymentRail, clock: Clock, tenant_id: TenantId, payment_id: PaymentId
) -> None:
    due = _still_due(store, clock, tenant_id, payment_id)
    if due is None:
        return
    if due.stop is None:
        charge = rail.debit(
            tenant_id,
            due.rail_token,
            due.payment.amount,
            payment_id,
            IdempotencyKey(f"debit_{payment_id}"),
        )
        if charge is None:
            # The rail answered for this Payment before, so this is a lookup fault.
            raise LookupError(f"the rail has no charge to debit for Payment {payment_id}")
    else:
        charge = rail.get_charge(tenant_id, payment_id)
        if charge is None or charge.rail_payment_id is None:  # no debit made: stop it
            log.info("Payment %s not debited: %s", payment_id, due.stop)
            charge = RailCharge(
                payment_id=payment_id,
                status="failed",
                amount=due.payment.amount,
                failure_code=due.stop,
            )
        else:  # a debit whose answer was lost: the money moved, so it settles as reported
            log.warning("Payment %s was debited before %s; not stopping it", payment_id, due.stop)
    with store.transaction(tenant_id) as tx:
        apply_charge(tx, payment_id, charge, decision_time(clock.now()))


def _still_due(
    store: SweepStore, clock: Clock, tenant_id: TenantId, payment_id: PaymentId
) -> _Due | None:
    """The Payment to debit and whether the Mandate and Agent still allow it, or None if
    it was settled or debited since it was listed."""
    now = decision_time(clock.now())
    with store.transaction(tenant_id) as tx:
        payment = tx.get_payment(payment_id)
        if payment is None or not payment.debit_due(now):
            return None
        mandate = tx.lock_mandate(payment.mandate_id)
        decision = tx.get_decision(payment.decision_id)
        if mandate is None or decision is None or mandate.rail_token is None:
            raise RuntimeError(f"Payment {payment_id} lost its Mandate, Decision or Rail token")
        agent = tx.get_agent(decision.agent_id)
        stop = debit_refusal(
            mandate_status=mandate.status(now),
            agent_disabled=agent is None or agent.status == "disabled",
        )
        return _Due(payment, mandate.rail_token, stop)
