"""SettlingMockRail: what the worker's reconciliation sweep asks on the hosted demo, with
`AGENTPAY_MOCK_SETTLE_AFTER_SECONDS` set (#135, ADR-0019).

MockRail keeps its charges inside the API process, so the worker can't ask it. This rail
answers from the stored Payment instead: every mock Payment that was debited is captured.
The sweep only asks about Payments older than the setting, and applies the answer through
the same transition a rail webhook uses (audit record, outbox event, Tenant webhook).

It is for the demo only. Settings refuse it with any real rail, and it never makes,
debits or refunds a charge.
"""

from datetime import timedelta

from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
from core.domain.rails import RailToken
from core.ports import IdempotencyKey, PaymentRail, RailCharge, UnitOfWork


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
        # Mock Payments are debited when the API schedules them; nothing is ever due.
        return self.get_charge(tenant_id, payment_id)

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
        amount: Money,
        idempotency_key: IdempotencyKey,
    ) -> RailCharge:
        raise NotImplementedError("the settling rail only answers the worker's sweep")


_conforms: type[PaymentRail] = SettlingMockRail  # mypy checks it matches the port
