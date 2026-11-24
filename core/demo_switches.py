"""Demo control switches (#281, ADR-0032): forced failures for the Demo Tenant on the mock
rail. `SwitchedRail` wraps the mock rail, so the API and the worker see the same switches
from the `demo_controls` table, and every other Tenant's calls pass straight through.

- `rail_down`: for the outage's length, the rail raises `RailUnavailable` for the Demo
  Tenant, as a rail that stopped answering does.
- `rail_declining`: a new charge or refund under the Mandate answers failed.
- `lose_payment`: a new charge under the Mandate never reaches the rail. Its outcome is
  unknown, so the Payment stays scheduled until Reconciliation fails it with
  `RAIL_CHARGE_MISSING`.

`switchable_writer` keeps the model out of a Dispute narrative when `ai_off` is on for the
Mandate, so the pack falls back to the template.
"""

from datetime import datetime, timedelta

from core.domain.demo_controls import RAIL_TARGET, DemoControlKind
from core.domain.evidence import EvidenceBundle
from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
from core.domain.rails import RailToken
from core.domain.refunds import RefundId
from core.evidence import ModelNarrative, NarrativeWriter
from core.ports import (
    Clock,
    IdempotencyKey,
    PaymentRail,
    RailCharge,
    RailRefund,
    RailUnavailable,
    TenantTransaction,
    UnitOfWork,
)

# Razorpay's code for a declined debit, which the mock rail also answers.
DECLINED = "BAD_REQUEST_ERROR"


def outage_ends_at(tx: TenantTransaction, outage: timedelta, now: datetime) -> datetime | None:
    """When the Demo Tenant's running rail outage ends, or None if none is running."""
    control = tx.latest_demo_control("rail_down", RAIL_TARGET)
    if control is None or now >= control.at + outage:
        return None
    return control.at + outage


class SwitchedRail:
    def __init__(
        self,
        inner: PaymentRail,
        store: UnitOfWork,
        demo_tenant: TenantId,
        clock: Clock,
        outage: timedelta,
    ) -> None:
        self._inner = inner
        self._store = store
        self._demo_tenant = demo_tenant
        self._clock = clock
        self._outage = outage
        self.lookup_lag = inner.lookup_lag

    def _check_up(self, tenant_id: TenantId) -> None:
        if tenant_id != self._demo_tenant:
            return
        with self._store.transaction(tenant_id) as tx:
            ends = outage_ends_at(tx, self._outage, self._clock.now())
        if ends is not None:
            raise RailUnavailable("simulated rail outage (Demo control)")

    def _on(self, tenant_id: TenantId, kind: DemoControlKind, payment_id: PaymentId) -> bool:
        """Whether this switch is on for the Payment's Mandate."""
        if tenant_id != self._demo_tenant:
            return False
        with self._store.transaction(tenant_id) as tx:
            payment = tx.get_payment(payment_id)
            return payment is not None and tx.get_demo_control(kind, payment.mandate_id) is not None

    def schedule_charge(
        self,
        tenant_id: TenantId,
        rail_token: RailToken,
        amount: Money,
        payment_id: PaymentId,
        idempotency_key: IdempotencyKey,
    ) -> RailCharge:
        self._check_up(tenant_id)
        if self._inner.get_charge(tenant_id, payment_id) is None:
            if self._on(tenant_id, "rail_declining", payment_id):
                return _declined(payment_id, amount)
            if self._on(tenant_id, "lose_payment", payment_id):
                raise RailUnavailable("simulated lost payment (Demo control)")
        return self._inner.schedule_charge(
            tenant_id, rail_token, amount, payment_id, idempotency_key
        )

    def debit(
        self,
        tenant_id: TenantId,
        rail_token: RailToken,
        amount: Money,
        payment_id: PaymentId,
        idempotency_key: IdempotencyKey,
    ) -> RailCharge | None:
        self._check_up(tenant_id)
        if self._inner.get_charge(tenant_id, payment_id) is None and self._on(
            tenant_id, "rail_declining", payment_id
        ):
            return _declined(payment_id, amount)
        return self._inner.debit(tenant_id, rail_token, amount, payment_id, idempotency_key)

    def get_charge(self, tenant_id: TenantId, payment_id: PaymentId) -> RailCharge | None:
        self._check_up(tenant_id)
        return self._inner.get_charge(tenant_id, payment_id)

    def refund(
        self,
        tenant_id: TenantId,
        payment_id: PaymentId,
        refund_id: RefundId,
        amount: Money,
        idempotency_key: IdempotencyKey,
    ) -> RailRefund:
        self._check_up(tenant_id)
        if self._inner.get_refund(tenant_id, payment_id, refund_id) is None and self._on(
            tenant_id, "rail_declining", payment_id
        ):
            return RailRefund(
                refund_id=refund_id, status="failed", amount=amount, failure_code=DECLINED
            )
        return self._inner.refund(tenant_id, payment_id, refund_id, amount, idempotency_key)

    def get_refund(
        self, tenant_id: TenantId, payment_id: PaymentId, refund_id: RefundId
    ) -> RailRefund | None:
        self._check_up(tenant_id)
        return self._inner.get_refund(tenant_id, payment_id, refund_id)


def _declined(payment_id: PaymentId, amount: Money) -> RailCharge:
    return RailCharge(payment_id=payment_id, status="failed", amount=amount, failure_code=DECLINED)


def switchable_writer(
    writer: NarrativeWriter, store: UnitOfWork, demo_tenant: TenantId
) -> NarrativeWriter:
    """`writer`, except that with `ai_off` on for the Dispute's Mandate no model is called
    and the pack falls back to the template narrative."""

    def write(tenant_id: TenantId, bundle: EvidenceBundle) -> ModelNarrative:
        if tenant_id == demo_tenant:
            with store.transaction(tenant_id) as tx:
                off = tx.get_demo_control("ai_off", bundle.mandate.mandate_id) is not None
            if off:
                return ModelNarrative(None, ())
        return writer(tenant_id, bundle)

    return write


_conforms: type[PaymentRail] = SwitchedRail  # mypy checks it matches the port
