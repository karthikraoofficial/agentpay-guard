"""One deployment, several rails (#300, ADR-0033). `RoutedRail` sends each call to the rail
its Payment is on: a new charge or a debit by the Mandate's Rail token, and a lookup or
refund by the rail recorded on the stored Payment. Callers see one `PaymentRail`."""

from collections.abc import Mapping
from datetime import timedelta

from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
from core.domain.rails import RailName, RailToken
from core.domain.refunds import RefundId
from core.ports import (
    IdempotencyKey,
    PaymentRail,
    RailCharge,
    RailRefund,
    RailUnavailable,
    UnitOfWork,
)


class RoutedRail:
    def __init__(self, rails: Mapping[RailName, PaymentRail], store: UnitOfWork) -> None:
        if not rails:
            raise ValueError("a deployment reaches at least one rail")
        self._rails = dict(rails)
        self._store = store
        # Never sooner than the slowest rail can see a new charge.
        self.lookup_lag: timedelta = max(r.lookup_lag for r in self._rails.values())

    @property
    def rails(self) -> Mapping[RailName, PaymentRail]:
        """Each rail this one routes to, by name."""
        return dict(self._rails)

    def _for_token(self, rail_token: RailToken) -> PaymentRail:
        rail = self._rails.get(rail_token.rail)
        if rail is None:  # payment_refusal refuses these before any rail call
            raise RailUnavailable(f"this deployment doesn't reach the {rail_token.rail} rail")
        return rail

    def _for_payment(self, tenant_id: TenantId, payment_id: PaymentId) -> PaymentRail | None:
        """The rail the Payment is on, or None if there is no such Payment."""
        if len(self._rails) == 1:
            return next(iter(self._rails.values()))
        with self._store.transaction(tenant_id) as tx:
            payment = tx.get_payment(payment_id)
        if payment is None:
            return None
        rail = self._rails.get(payment.rail)
        if rail is None:
            raise RailUnavailable(f"this deployment doesn't reach the {payment.rail} rail")
        return rail

    def schedule_charge(
        self,
        tenant_id: TenantId,
        rail_token: RailToken,
        amount: Money,
        payment_id: PaymentId,
        idempotency_key: IdempotencyKey,
    ) -> RailCharge:
        rail = self._for_token(rail_token)
        return rail.schedule_charge(tenant_id, rail_token, amount, payment_id, idempotency_key)

    def debit(
        self,
        tenant_id: TenantId,
        rail_token: RailToken,
        amount: Money,
        payment_id: PaymentId,
        idempotency_key: IdempotencyKey,
    ) -> RailCharge | None:
        rail = self._for_token(rail_token)
        return rail.debit(tenant_id, rail_token, amount, payment_id, idempotency_key)

    def get_charge(self, tenant_id: TenantId, payment_id: PaymentId) -> RailCharge | None:
        rail = self._for_payment(tenant_id, payment_id)
        return None if rail is None else rail.get_charge(tenant_id, payment_id)

    def refund(
        self,
        tenant_id: TenantId,
        payment_id: PaymentId,
        refund_id: RefundId,
        amount: Money,
        idempotency_key: IdempotencyKey,
    ) -> RailRefund:
        rail = self._for_payment(tenant_id, payment_id)
        if rail is None:
            raise LookupError(f"no Payment {payment_id} to refund")
        return rail.refund(tenant_id, payment_id, refund_id, amount, idempotency_key)

    def get_refund(
        self, tenant_id: TenantId, payment_id: PaymentId, refund_id: RefundId
    ) -> RailRefund | None:
        rail = self._for_payment(tenant_id, payment_id)
        return None if rail is None else rail.get_refund(tenant_id, payment_id, refund_id)
