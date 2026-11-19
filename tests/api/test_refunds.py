"""/v1/payments/{id}/refunds: a Tenant returns money on a captured Payment (#206, PRD #205).

On the mock rail a Refund is processed at once. The `api` fixture verifies every chain and
replays every Decision when each test ends, so `refund` records must keep the chain
intact."""

import hashlib
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from adapters.memory.rail import MockRail
from adapters.memory.workflow import InMemoryWorkflow
from adapters.postgres.store import PostgresStore
from api.main import create_app
from core.domain.payments import PaymentId
from core.ports import RailUnavailable
from tests.api.conftest import NOW, Api, MovableClock
from tests.api.test_disputes import DISPUTE_RULES, SECRET, Disputes
from tests.api.test_payments import token
from tests.api.test_rail_webhooks import event
from tests.api.test_tenants_payers_agents import assert_problem
from tests.conftest import make_settings
from tests.packs import release_test_pack


class Refunds(Disputes):
    """A Tenant with a manage key, whose Payments are captured and can be refunded."""

    def refund(self, payment_id: str, *, headers=None, key: str | None = None, **body: Any):  # type: ignore[no-untyped-def]
        path = f"/v1/payments/{payment_id}/refunds"
        headers = headers or self.manage
        if key is None:
            return self.api.post(path, headers, body)
        return self.api.client.post(path, headers={**headers, "Idempotency-Key": key}, json=body)


@pytest.fixture
def refunds(api: Api, tmp_path: Path) -> Refunds:
    # The Phase 1 decision rules, with default@1.4.0's Dispute rules (no Refund window
    # set, so the legacy 180 days apply).
    api.use_packs(release_test_pack(tmp_path, "1.0.1", {"disputes": DISPUTE_RULES}))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    return Refunds(api)


def test_a_captured_payment_is_refunded_in_full_at_once_on_the_mock_rail(
    refunds: Refunds,
) -> None:
    payment = refunds.captured()

    made = refunds.refund(payment["payment_id"], amount=payment["amount"])

    assert made.status_code == 201, made.text
    refund = made.json()
    assert refund["refund_id"].startswith("rfd_")
    assert refund["payment_id"] == payment["payment_id"]
    assert refund["amount"] == payment["amount"]
    assert refund["status"] == "processed"
    assert refund["rail_refund_id"]
    shown = refunds.payment(payment["payment_id"], headers=refunds.manage).json()
    assert shown["status"] == "captured"  # "captured" still means the money moved
    assert shown["refunded"] == payment["amount"]
    assert [r["refund_id"] for r in shown["refunds"]] == [refund["refund_id"]]


def inr(minor: int) -> dict[str, Any]:
    return {"minor": minor, "currency": "INR"}


def test_partial_refunds_add_up_and_never_pass_the_captured_amount(refunds: Refunds) -> None:
    payment = refunds.captured()
    captured = payment["amount"]["minor"]
    pid = payment["payment_id"]

    first = refunds.refund(pid, amount=inr(captured - 30_000))
    second = refunds.refund(pid, amount=inr(30_000))
    over = refunds.refund(pid, amount=inr(1))

    assert (first.status_code, second.status_code) == (201, 201)
    assert_problem(over, 409, "REFUND_EXCEEDS_CAPTURED")
    shown = refunds.payment(pid, headers=refunds.manage).json()
    assert shown["refunded"] == inr(captured)
    assert len(shown["refunds"]) == 2


def test_only_a_captured_payment_can_be_refunded(refunds: Refunds) -> None:
    scheduled = refunds.pay(refunds.allow()["decision_id"]).json()
    failed = refunds.pay(refunds.allow()["decision_id"]).json()
    refunds.deliver(event(failed, "payment.failed"), f"evt_{failed['payment_id']}")
    assert refunds.status(failed) == "failed"

    for payment in (scheduled, failed):
        assert_problem(
            refunds.refund(payment["payment_id"], amount=payment["amount"]),
            409,
            "PAYMENT_NOT_REFUNDABLE",
        )


def test_a_payment_under_dispute_is_not_refunded(refunds: Refunds) -> None:
    payment = refunds.captured()
    pid = payment["payment_id"]
    dispute_id = refunds.open(pid).json()["dispute_id"]

    assert_problem(refunds.refund(pid, amount=payment["amount"]), 409, "PAYMENT_DISPUTED")  # open
    assert refunds.build(dispute_id) == "built"
    assert_problem(
        refunds.refund(pid, amount=payment["amount"]), 409, "PAYMENT_DISPUTED"
    )  # in_review
    refunds.sign_off(dispute_id)
    assert_problem(refunds.refund(pid, amount=payment["amount"]), 409, "PAYMENT_DISPUTED")  # ready


def test_a_withdrawn_dispute_no_longer_blocks_a_refund(refunds: Refunds) -> None:
    payment = refunds.captured()
    dispute_id = refunds.open(payment["payment_id"]).json()["dispute_id"]
    refunds.act(dispute_id, "withdraw")

    made = refunds.refund(payment["payment_id"], amount=payment["amount"])

    assert made.status_code == 201, made.text


def test_a_refund_after_the_refund_window_is_refused(api: Api, tmp_path: Path) -> None:
    packs = release_test_pack(
        tmp_path, "1.0.1", {"disputes": DISPUTE_RULES, "refund_window_days": 30}
    )
    api.use_packs(packs)
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    refunds = Refunds(api)
    early, late = refunds.captured(), refunds.captured()

    api.clock.advance(timedelta(days=30))  # captured exactly 30 days ago: still open
    inside = refunds.refund(early["payment_id"], amount=early["amount"])
    api.clock.advance(timedelta(seconds=1))
    outside = refunds.refund(late["payment_id"], amount=late["amount"])

    assert inside.status_code == 201, inside.text
    assert_problem(outside, 409, "REFUND_WINDOW_CLOSED")


def test_a_pack_without_a_refund_window_reads_as_180_days(refunds: Refunds) -> None:
    payment = refunds.captured()

    refunds.api.clock.advance(timedelta(days=180, seconds=1))

    assert_problem(
        refunds.refund(payment["payment_id"], amount=payment["amount"]),
        409,
        "REFUND_WINDOW_CLOSED",
    )


@pytest.mark.parametrize(
    "amount",
    [inr(0), inr(-1), {"minor": 1_000, "currency": "USD"}],
    ids=["zero", "negative", "other-currency"],
)
def test_a_refund_amount_must_be_above_zero_in_the_payments_currency(
    refunds: Refunds, amount: dict[str, Any]
) -> None:
    payment = refunds.captured()

    refused = refunds.refund(payment["payment_id"], amount=amount)

    assert_problem(refused, 422, "VALIDATION_FAILED")
    assert refunds.payment(payment["payment_id"], headers=refunds.manage).json()["refunds"] == []


def test_another_tenants_payment_is_not_found(refunds: Refunds) -> None:
    payment = refunds.captured()
    other = Refunds(refunds.api, "globex")

    assert_problem(
        other.refund(payment["payment_id"], amount=payment["amount"]), 404, "PAYMENT_NOT_FOUND"
    )


def test_an_agents_decide_key_can_never_refund(refunds: Refunds) -> None:
    payment = refunds.captured()

    refused = refunds.refund(
        payment["payment_id"], headers=refunds.decide, amount=payment["amount"]
    )

    assert_problem(refused, 403, "INSUFFICIENT_SCOPE")


def test_a_replayed_refund_returns_the_same_refund_and_refunds_once(refunds: Refunds) -> None:
    payment = refunds.captured()
    pid = payment["payment_id"]

    first = refunds.refund(pid, key="refund-1", amount=inr(10_000))
    again = refunds.refund(pid, key="refund-1", amount=inr(10_000))

    assert again.status_code == first.status_code == 201
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert again.json() == first.json()
    assert refunds.api.rail.refunds_for(refunds.tenant_id(), PaymentId(pid)) == 1
    assert len(refunds.payment(pid, headers=refunds.manage).json()["refunds"]) == 1


def test_a_refund_writes_a_refund_record_when_asked_for_and_another_when_it_ends(
    refunds: Refunds,
) -> None:
    payment = refunds.captured()
    trail = refunds.trail(payment["decision_id"])
    decided = refunds.audit(trail["decision"][0]).record
    words = "Two items came back damaged"

    refund = refunds.refund(payment["payment_id"], amount=inr(20_000), reason=words).json()

    records = [refunds.audit(i) for i in refunds.trail(payment["decision_id"])["refund"]]
    assert [r.record.refund.status for r in records] == ["pending", "processed"]
    for sealed in records:
        assert sealed.record.decision_id == payment["decision_id"]
        evidence = sealed.record.refund
        assert evidence.refund_id == refund["refund_id"]
        assert evidence.amount.minor == 20_000
        assert evidence.reason_hash == "sha256:" + hashlib.sha256(words.encode()).hexdigest()
        assert words.encode() not in sealed.body  # the words never enter the chain
        # The pack active now; here the same one that made the Decision.
        assert evidence.policy_pack == decided.pack
    assert records[1].record.refund.rail_refund_id == refund["rail_refund_id"]


def test_one_refund_can_be_read_on_its_own_payment(refunds: Refunds) -> None:
    payment, other = refunds.captured(), refunds.captured()
    refund = refunds.refund(payment["payment_id"], amount=inr(5_000)).json()

    def read(payment_id: str, refund_id: str):  # type: ignore[no-untyped-def]
        path = f"/v1/payments/{payment_id}/refunds/{refund_id}"
        return refunds.api.client.get(path, headers=refunds.manage)

    shown = read(payment["payment_id"], refund["refund_id"])
    assert shown.status_code == 200
    assert shown.json() == refund
    assert_problem(read(payment["payment_id"], "rfd_unknown"), 404, "REFUND_NOT_FOUND")
    assert_problem(read(other["payment_id"], refund["refund_id"]), 404, "REFUND_NOT_FOUND")


def test_a_refund_the_rail_refuses_fails_and_holds_nothing_back(refunds: Refunds) -> None:
    payment = refunds.captured()
    pid = payment["payment_id"]
    refunds.api.rail.forget(refunds.tenant_id(), PaymentId(pid))  # the rail refuses refunds

    first = refunds.refund(pid, amount=payment["amount"])
    second = refunds.refund(pid, amount=payment["amount"])  # the failed one doesn't count

    for made in (first, second):
        assert made.status_code == 201
        assert made.json()["status"] == "failed"
        assert made.json()["failure_code"]
    shown = refunds.payment(pid, headers=refunds.manage).json()
    assert shown["refunded"] == inr(0)
    statuses = [
        refunds.audit(i).record.refund.status
        for i in refunds.trail(payment["decision_id"])["refund"]
    ]
    assert statuses == ["pending", "failed", "pending", "failed"]


# --- Concurrency (Postgres: the in-memory store doesn't model parallel writers) ----------


def test_two_refunds_at_once_queue_on_the_payment_and_never_overshoot(
    migrated_database_url: str, tmp_path: Path
) -> None:
    store = PostgresStore(create_engine(migrated_database_url, pool_size=6))
    clock = MovableClock(NOW)
    packs = release_test_pack(tmp_path, "1.0.1", {"disputes": DISPUTE_RULES})
    settings = make_settings(policy_packs_dir=packs, razorpay_webhook_secret=SECRET)
    workflow, rail = InMemoryWorkflow(), MockRail()
    app = create_app(settings, store=store, clock=clock, workflow=workflow, rail=rail)
    api = Api(TestClient(app), store, clock, workflow=workflow, rail=rail, packs=packs)
    refunds = Refunds(api)
    payment = refunds.captured()
    most = inr(payment["amount"]["minor"] * 6 // 10)  # two of these overshoot
    start = threading.Barrier(2)
    statuses: list[int] = []
    lock = threading.Lock()

    def refund(key: str) -> None:
        start.wait()
        status = refunds.refund(payment["payment_id"], key=key, amount=most).status_code
        with lock:
            statuses.append(status)

    workers = [threading.Thread(target=refund, args=(f"race-{i}",)) for i in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)

    assert sorted(statuses) == [201, 409]
    assert rail.refunds_for(refunds.tenant_id(), PaymentId(payment["payment_id"])) == 1
    api.assert_evidence_holds()


# --- Spent this month (#207) ---------------------------------------------------------


def tight(refunds: Refunds, cap: int) -> str:
    """A Mandate whose Monthly cap and per-transaction cap are both `cap` rupees."""
    mandate = refunds.create(
        rail_token=token(MockRail.token_that_captures()),
        per_txn_cap=inr(cap * 100),
        monthly_cap=inr(cap * 100),
    )
    mandate_id: str = mandate["mandate_id"]
    return mandate_id


def spent_seen_by(refunds: Refunds, decision: dict[str, Any]) -> int:
    """Spent this month as the Decision's Audit record says the engine saw it."""
    spent: int = refunds.audit(decision["audit_record_id"]).record.inputs.spent_this_month.minor
    return spent


def test_a_processed_refund_gives_back_the_monthly_cap_it_used(refunds: Refunds) -> None:
    mandate_id = tight(refunds, 2_000)
    payment = refunds.captured(mandate_id=mandate_id)  # INR 1,250 of 2,000
    over = refunds.ask(mandate_id=mandate_id).json()
    assert (over["outcome"], over["reason_codes"][0]) == ("block", "MONTHLY_CAP_EXCEEDED")

    refunds.refund(payment["payment_id"], amount=payment["amount"])

    after = refunds.ask(mandate_id=mandate_id).json()
    assert after["outcome"] == "allow"
    assert spent_seen_by(refunds, after) == 0


def test_a_partial_refund_gives_back_exactly_its_amount(refunds: Refunds) -> None:
    mandate_id = tight(refunds, 2_000)
    payment = refunds.captured(mandate_id=mandate_id)

    refunds.refund(payment["payment_id"], amount=inr(500_00))

    later = refunds.allow(mandate_id=mandate_id, amount=inr(100_00))
    assert spent_seen_by(refunds, later) == 1_250_00 - 500_00


def test_a_failed_refund_gives_back_nothing(refunds: Refunds) -> None:
    mandate_id = tight(refunds, 2_000)
    payment = refunds.captured(mandate_id=mandate_id)
    refunds.api.rail.forget(refunds.tenant_id(), PaymentId(payment["payment_id"]))

    failed = refunds.refund(payment["payment_id"], amount=payment["amount"]).json()

    assert failed["status"] == "failed"
    later = refunds.allow(mandate_id=mandate_id, amount=inr(100_00))
    assert spent_seen_by(refunds, later) == 1_250_00


def test_a_pending_refund_gives_back_nothing(
    refunds: Refunds, monkeypatch: pytest.MonkeyPatch
) -> None:
    mandate_id = tight(refunds, 2_000)
    payment = refunds.captured(mandate_id=mandate_id)

    def times_out(*args: Any) -> None:
        raise RailUnavailable("timed out")

    monkeypatch.setattr(refunds.api.rail, "refund", times_out)
    pending = refunds.refund(payment["payment_id"], amount=payment["amount"]).json()

    assert pending["status"] == "pending"
    later = refunds.allow(mandate_id=mandate_id, amount=inr(100_00))
    assert spent_seen_by(refunds, later) == 1_250_00


def test_refunding_last_months_payment_frees_nothing_this_month(refunds: Refunds) -> None:
    mandate_id = tight(refunds, 2_000)
    refunds.api.clock.advance(timedelta(days=28, hours=14, minutes=25))  # 2026-11-30 23:55Z
    november = refunds.captured(mandate_id=mandate_id)
    refunds.api.clock.advance(timedelta(minutes=7))  # 2026-12-01 00:02Z
    december = refunds.captured(mandate_id=mandate_id, amount=inr(500_00))

    refunds.refund(november["payment_id"], amount=november["amount"])

    later = refunds.allow(mandate_id=mandate_id, amount=inr(100_00))
    assert spent_seen_by(refunds, later) == december["amount"]["minor"]
