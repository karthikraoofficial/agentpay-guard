"""MockRail: an in-process PaymentRail that behaves like Razorpay card recurring payments.

A charge is scheduled first and settles only when `settle` is called, as a real debit
settles hours later (ADR-0013). Charges are keyed by Payment id (ADR-0014).

By default the debit is made when the charge is scheduled. Given a `notice` period and a
clock, it behaves like a card debit after a Pre-debit notice (ADR-0017): scheduling only
books the charge with `debit_after`, and `debit` makes it once the period is over. A
notice can fail (`token_whose_notice_fails`): the charge is then failed with no debit.
"""

import threading
from datetime import datetime, timedelta
from typing import Literal

from core.domain.money import Money
from core.domain.parties import TenantId
from core.domain.payments import PaymentId
from core.domain.rails import RAIL_NOTICE_FAILED, RailToken
from core.domain.refunds import RefundId
from core.ports import (
    Clock,
    IdempotencyKey,
    PaymentRail,
    RailCharge,
    RailRefund,
    RailUnavailable,
)

# Token refs that decide how a mock debit ends. Any other ref captures.
_CAPTURES = "token_mock_captures"
_FAILS = "token_mock_fails"  # accepted, then the debit fails when it settles
_REFUSED = "token_mock_refused"  # refused outright: failed, with nothing to settle
_TIMES_OUT = "token_mock_times_out"  # the charge is made, then the call times out
_NOTICE_FAILS = "token_mock_notice_fails"  # its Pre-debit notice fails after scheduling
_FAILURE_CODE = "BAD_REQUEST_ERROR"  # Razorpay's code for a declined debit


class MockRail:
    lookup_lag = timedelta(0)  # its charges are in a dict, seen at once

    def __init__(self, *, notice: timedelta | None = None, clock: Clock | None = None) -> None:
        if (notice is None) != (clock is None):
            raise ValueError("a notice period needs a clock, and a clock a notice period")
        self._notice = notice
        self._clock = clock
        self._charges: dict[tuple[TenantId, PaymentId], RailCharge] = {}
        self._tokens: dict[tuple[TenantId, PaymentId], RailToken] = {}
        self.charges_made = 0
        self._made: dict[tuple[TenantId, PaymentId], int] = {}
        self._booked: dict[tuple[TenantId, PaymentId], int] = {}
        # (tenant, Refund id) -> (its Payment, the answer given)
        self._refunds: dict[tuple[TenantId, RefundId], tuple[PaymentId, RailRefund]] = {}
        self._refunds_made: dict[tuple[TenantId, PaymentId], int] = {}
        self._hold_refunds = False
        self._time_out_refunds = False
        self._lock = threading.Lock()  # the API calls the rail from several threads

    @staticmethod
    def token_that_captures() -> RailToken:
        return _token(_CAPTURES)

    @staticmethod
    def token_that_fails() -> RailToken:
        return _token(_FAILS)

    @staticmethod
    def token_that_is_refused() -> RailToken:
        return _token(_REFUSED)

    @staticmethod
    def token_that_times_out() -> RailToken:
        return _token(_TIMES_OUT)

    @staticmethod
    def token_whose_notice_fails() -> RailToken:
        """Only with a notice period; without one there is no notice, and it captures."""
        return _token(_NOTICE_FAILS)

    def schedule_charge(
        self,
        tenant_id: TenantId,
        rail_token: RailToken,
        amount: Money,
        payment_id: PaymentId,
        idempotency_key: IdempotencyKey,
    ) -> RailCharge:
        with self._lock:
            return self._schedule(tenant_id, rail_token, amount, payment_id)

    def _schedule(
        self, tenant_id: TenantId, rail_token: RailToken, amount: Money, payment_id: PaymentId
    ) -> RailCharge:
        key = (tenant_id, payment_id)
        if (existing := self._charges.get(key)) is not None:
            return existing  # one Rail charge per Payment (ADR-0014)
        self._booked[key] = self._booked.get(key, 0) + 1
        if rail_token.token_ref == _REFUSED:
            charge = RailCharge(
                payment_id=payment_id, status="failed", amount=amount, failure_code=_FAILURE_CODE
            )
        elif self._notice is not None and self._clock is not None:
            charge = RailCharge(
                payment_id=payment_id,
                status="scheduled",
                amount=amount,
                debit_after=self._clock.now() + self._notice,
            )
        else:
            charge = self._debited(key, amount)
        notice_fails = self._notice is not None and rail_token.token_ref == _NOTICE_FAILS
        # A failed notice fails after this answer: every later read sees the charge failed.
        self._charges[key] = (
            charge.model_copy(update={"status": "failed", "failure_code": RAIL_NOTICE_FAILED})
            if notice_fails
            else charge
        )
        self._tokens[key] = rail_token
        if rail_token.token_ref == _TIMES_OUT:
            raise RailUnavailable("mock rail timed out after making the charge")
        return charge

    def _debited(self, key: tuple[TenantId, PaymentId], amount: Money) -> RailCharge:
        self.charges_made += 1
        self._made[key] = self._made.get(key, 0) + 1
        return RailCharge(
            payment_id=key[1],
            status="scheduled",
            amount=amount,
            rail_payment_id=f"pay_mock_{self.charges_made:06d}",
        )

    def debit(
        self,
        tenant_id: TenantId,
        rail_token: RailToken,
        amount: Money,
        payment_id: PaymentId,
        idempotency_key: IdempotencyKey,
    ) -> RailCharge | None:
        with self._lock:
            key = (tenant_id, payment_id)
            charge = self._charges.get(key)
            if charge is None or charge.rail_payment_id is not None or charge.status != "scheduled":
                return charge  # nothing to debit, or debited already: never twice
            if not _over(charge.debit_after, self._clock):
                return charge  # the notice period isn't over: not yet
            debited = self._debited(key, amount).model_copy(
                update={"debit_after": charge.debit_after}
            )
            self._charges[key] = debited
            return debited

    def get_charge(self, tenant_id: TenantId, payment_id: PaymentId) -> RailCharge | None:
        with self._lock:
            return self._charges.get((tenant_id, payment_id))

    def forget(self, tenant_id: TenantId, payment_id: PaymentId) -> None:
        """Test hook: the rail loses track of a charge it made, as a faulty lookup would."""
        with self._lock:
            self._charges.pop((tenant_id, payment_id), None)

    def charges_for(self, tenant_id: TenantId, payment_id: PaymentId) -> int:
        """Test hook: how many debits were ever made for this Payment."""
        with self._lock:
            return self._made.get((tenant_id, payment_id), 0)

    def bookings_for(self, tenant_id: TenantId, payment_id: PaymentId) -> int:
        """Test hook: how many charges were ever booked for this Payment, debited or not."""
        with self._lock:
            return self._booked.get((tenant_id, payment_id), 0)

    def refund(
        self,
        tenant_id: TenantId,
        payment_id: PaymentId,
        refund_id: RefundId,
        amount: Money,
        idempotency_key: IdempotencyKey,
    ) -> RailRefund:
        """Processed at once, from a charge this rail debited, never beyond its amount in
        all. A refund made is found again by its Refund id; a refused one is not kept. The
        charges live in this
        process, so a Payment debited before a restart can't be refunded (it fails).
        After `hold_refunds` a refund answers pending until `finish_refund`; after
        `time_out_refunds` it is made and then the call times out."""
        with self._lock:
            done = self._refunds.get((tenant_id, refund_id))
            if done is not None:
                return done[1]
            charge = self._charges.get((tenant_id, payment_id))
            refunded = sum(
                r.amount.minor
                for (t, _), (p, r) in self._refunds.items()
                if (t, p) == (tenant_id, payment_id) and r.status != "failed"
            )
            ok = (
                charge is not None
                and charge.rail_payment_id is not None
                and charge.amount.currency == amount.currency
                and refunded + amount.minor <= charge.amount.minor
            )
            answer = RailRefund(
                refund_id=refund_id,
                status=("pending" if self._hold_refunds else "processed") if ok else "failed",
                amount=amount,
                rail_refund_id=f"rfnd_mock_{refund_id}" if ok else None,
                failure_code=None if ok else _FAILURE_CODE,
            )
            if not ok:
                return answer  # refused: no refund was made, so there's none to find later
            self._refunds[(tenant_id, refund_id)] = (payment_id, answer)
            made = self._refunds_made.get((tenant_id, payment_id), 0)
            self._refunds_made[(tenant_id, payment_id)] = made + 1
            if self._time_out_refunds:
                raise RailUnavailable("mock refund timed out after it was made")
            return answer

    def get_refund(
        self, tenant_id: TenantId, payment_id: PaymentId, refund_id: RefundId
    ) -> RailRefund | None:
        with self._lock:
            done = self._refunds.get((tenant_id, refund_id))
        return None if done is None else done[1]

    def hold_refunds(self) -> None:
        """Test hook: refunds from now on answer pending, as a slow rail's do."""
        self._hold_refunds = True

    def time_out_refunds(self) -> None:
        """Test hook: refunds from now on are made, then the call times out."""
        self._time_out_refunds = True

    def finish_refund(
        self, tenant_id: TenantId, refund_id: RefundId, status: Literal["processed", "failed"]
    ) -> None:
        """Test hook: the rail finishes a held refund, as a slow rail does later."""
        with self._lock:
            payment_id, held = self._refunds[(tenant_id, refund_id)]
            update: dict[str, object] = {"status": status}
            if status == "failed":
                update |= {"failure_code": _FAILURE_CODE}
                made = self._refunds_made[(tenant_id, payment_id)]
                self._refunds_made[(tenant_id, payment_id)] = made - 1
            self._refunds[(tenant_id, refund_id)] = (payment_id, held.model_copy(update=update))

    def refunds_for(self, tenant_id: TenantId, payment_id: PaymentId) -> int:
        """Test hook: how many refunds were ever made on this Payment's charge."""
        with self._lock:
            return self._refunds_made.get((tenant_id, payment_id), 0)

    def settle(self, tenant_id: TenantId, payment_id: PaymentId) -> None:
        """Test hook: the rail finishes the debit, as Razorpay does hours later."""
        with self._lock:
            self._settle((tenant_id, payment_id))

    def _settle(self, key: tuple[TenantId, PaymentId]) -> None:
        charge = self._charges[key]
        if charge.status != "scheduled" or charge.rail_payment_id is None:
            return  # settled already, or no debit made yet
        if self._tokens[key].token_ref == _FAILS:
            settled = charge.model_copy(update={"status": "failed", "failure_code": _FAILURE_CODE})
        else:
            settled = charge.model_copy(update={"status": "captured"})
        self._charges[key] = settled


def _over(debit_after: datetime | None, clock: Clock | None) -> bool:
    return debit_after is None or clock is None or clock.now() >= debit_after


def _token(token_ref: str) -> RailToken:
    return RailToken(
        rail="mock", customer_ref="cust_mock", token_ref=token_ref, display="visa •••• 1111"
    )


_conforms: type[PaymentRail] = MockRail  # mypy checks the fake matches the port
