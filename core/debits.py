"""The debit step: debit the card for each Payment whose Pre-debit notice period has ended
(ADR-0017). The worker runs it before each reconciliation sweep.

Just before the debit it checks again that the Mandate is active and the Agent enabled,
as a payment does. If either no longer holds, the Payment fails with that Reason code and
its Decision is Voided, so a Revocation during the notice period stops the debit. The rail
call is outside any transaction; its answer goes through `apply_charge`, the same
transition a rail webhook uses. A rail error is logged and the Payment tried again next
time: the rail never debits one Payment twice.
"""

import logging
from dataclasses import dataclass

from core.charges import apply_charge
from core.domain.decisions import decision_time
from core.domain.payments import Payment, PaymentId, debit_refusal
from core.domain.rails import RailToken
from core.ports import Clock, IdempotencyKey, PaymentRail, RailCharge, TenantId
from core.reconcile import SweepStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DebitResult:
    checked: int
    errors: int


def debit_due(store: SweepStore, rail: PaymentRail, clock: Clock) -> DebitResult:
    now = decision_time(clock.now())
    due = store.due_debits(now)
    errors = 0
    for tenant_id, payment_id in due:
        try:
            ready = _checked(store, tenant_id, payment_id, clock)
            if ready is None:
                continue
            payment, rail_token = ready
            charge = rail.debit(
                tenant_id,
                rail_token,
                payment.amount,
                payment_id,
                IdempotencyKey(f"debit_{payment_id}"),
            )
            if charge is None:
                # The rail answered for this Payment before, so this is a lookup fault.
                raise LookupError(f"the rail has no charge to debit for Payment {payment_id}")
            with store.transaction(tenant_id) as tx:
                apply_charge(tx, payment_id, charge, decision_time(clock.now()))
        except Exception as exc:  # one Payment's trouble never stops the step
            errors += 1
            log.error("debit of Payment %s failed: %r", payment_id, exc, exc_info=exc)
    return DebitResult(checked=len(due), errors=errors)


def _checked(
    store: SweepStore, tenant_id: TenantId, payment_id: PaymentId, clock: Clock
) -> tuple[Payment, RailToken] | None:
    """The Payment and the Rail token to debit, if the debit is still due and allowed.
    Otherwise None, after failing the Payment if the Mandate or Agent stops it."""
    now = decision_time(clock.now())
    with store.transaction(tenant_id) as tx:
        payment = tx.get_payment(payment_id)
        if payment is None or not payment.debit_due(now):
            return None  # settled or debited since it was listed
        mandate = tx.lock_mandate(payment.mandate_id)
        decision = tx.get_decision(payment.decision_id)
        if mandate is None or decision is None or mandate.rail_token is None:
            raise RuntimeError(f"Payment {payment_id} lost its Mandate, Decision or Rail token")
        agent = tx.get_agent(decision.agent_id)
        stop = debit_refusal(
            mandate_status=mandate.status(now),
            agent_disabled=agent is None or agent.status == "disabled",
        )
        if stop is None:
            return payment, mandate.rail_token
        log.info("Payment %s not debited: %s", payment_id, stop)
        apply_charge(
            tx,
            payment_id,
            RailCharge(
                payment_id=payment_id, status="failed", amount=payment.amount, failure_code=stop
            ),
            now,
        )
        return None
