"""SettlingMockRail: what the worker's reconciliation sweep asks on the hosted demo, with
`AGENTPAY_MOCK_SETTLE_AFTER_SECONDS` set (#135, ADR-0019).

MockRail keeps its charges inside the API process, so the worker can't ask it. This rail
answers from the stored Payment instead: every mock Payment that was debited is captured.
The sweep only asks about Payments older than the setting, and applies the answer through
the same transition a rail webhook uses (audit record, outbox event, Tenant webhook).

With `AGENTPAY_MOCK_NOTICE_SECONDS` set (#280), the API's MockRail only books each
charge, and this rail makes the debit when the worker's debit step asks, once the notice
period is over (or a Demo control ended it, ADR-0032): the debit step decides when. A
charge on the notice-fails test card fails with RAIL_NOTICE_FAILED instead, as Razorpay
reports a failed Pre-debit notice.

It is for the demo only. Settings refuse it with any real rail, and it never makes a
charge or a refund.
"""

from datetime import timedelta

from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
from core.domain.rails import RAIL_NOTICE_FAILED, RailToken
from core.domain.refunds import RefundId
from core.ports import IdempotencyKey, PaymentRail, RailCharge, RailRefund, UnitOfWork

# The mock test card whose Pre-debit notice fails (adapters/memory/rail.py).
NOTICE_FAILS = "token_mock_notice_fails"


class SettlingMockRail:
    lookup_lag = timedelta(0)

    def __init__(self, store: UnitOfWork) -> None:
        self._store = store

    def get_charge(self, tenant_id: TenantId, payment_id: PaymentId) -> RailCharge | None:
        with self._store.transaction(tenant_id) as tx:
            payment = tx.get_payment(payment_id)
        if payment is None or payment.rail != "mock" or payment.rail_payment_id is None:
            # Never debited: the sweep fails it as a charge that never reached the rail.
            return None
        return RailCharge(
            payment_id=payment_id,
            status="captured",
            amount=payment.amount,
            rail_payment_id=payment.rail_payment_id,
            debit_after=payment.debit_after,
        )

    def debit(
        self,
        tenant_id: TenantId,
        rail_token: RailToken,
        amount: Money,
        payment_id: PaymentId,
        idempotency_key: IdempotencyKey,
    ) -> RailCharge | None:
        with self._store.transaction(tenant_id) as tx:
            payment = tx.get_payment(payment_id)
        if payment is None or payment.rail != "mock":
            return None
        if payment.rail_payment_id is not None:  # debited already: never twice
            return self.get_charge(tenant_id, payment_id)
        if rail_token.token_ref == NOTICE_FAILS:
            return RailCharge(
                payment_id=payment_id,
                status="failed",
                amount=amount,
                failure_code=RAIL_NOTICE_FAILED,
                debit_after=payment.debit_after,
            )
        # The debit is made now; the sweep captures it once it is old enough.
        return RailCharge(
            payment_id=payment_id,
            status="scheduled",
            amount=amount,
            rail_payment_id=f"pay_mock_{payment_id}",
            debit_after=payment.debit_after,
        )

    def schedule_charge(
        self,
        tenant_id: TenantId,
        rail_token: RailToken,
        amount: Money,
        payment_id: PaymentId,
        idempotency_key: IdempotencyKey,
    ) -> RailCharge:
        raise NotImplementedError("the settling rail only answers the worker's sweep")

    def refund(
        self,
        tenant_id: TenantId,
        payment_id: PaymentId,
        refund_id: RefundId,
        amount: Money,
        idempotency_key: IdempotencyKey,
    ) -> RailRefund:
        # Refunds are made by the API's MockRail; this rail only answers the sweep.
        raise NotImplementedError("the settling rail only answers the worker's sweep")

    def get_refund(
        self, tenant_id: TenantId, payment_id: PaymentId, refund_id: RefundId
    ) -> RailRefund | None:
        # The API's MockRail answers every refund at once, so a pending mock Refund is one
        # the API stopped before asking about: the rail never had it, and the sweep fails it.
        return None


_conforms: type[PaymentRail] = SettlingMockRail  # mypy checks it matches the port
