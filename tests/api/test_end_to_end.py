"""End to end on the mock rail: demo journey 2 (Phase 4), an allow becomes a captured
Payment; and demo journey 3 (Phase 5), that Payment is disputed and its Evidence pack is
cited, signed off and verified.

A real test-mode card debit can't complete, because Razorpay waits 36 h 5 min after the
Pre-debit notice and a test token expires before that (ADR-0017). So this runs the whole
path on the mock rail with the same notice period and a clock the test moves. The real
Razorpay half (allow, `scheduled`, notice delivered) is the runbook in the README."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from adapters.memory.rail import MockRail
from api.admin import IDENTICAL, verify_chain, verify_pack_command
from core.debits import run_debit_step
from core.domain.payments import PaymentId
from core.reconcile import sweep
from tests.api.conftest import Api
from tests.api.test_debits import NOTICE, SWEEP_AGE
from tests.api.test_disputes import DISPUTE_RULES, REVIEWER, DisputeHooks, Reviews
from tests.api.test_rail_webhooks import SECRET, Webhooks, event
from tests.api.test_tenant_webhooks import Hooks, verifies
from tests.conftest import make_settings
from tests.packs import phase_1_packs, release_test_pack


class Journey(Hooks, Webhooks):
    """One Tenant's way through: a Mandate whose Rail token captures, a registered
    webhook endpoint, and a way to send signed Razorpay events."""


@pytest.fixture
def journey(api: Api, tmp_path: Path) -> Journey:
    api.rail = MockRail(notice=NOTICE, clock=api.clock)
    api.use_packs(phase_1_packs(tmp_path))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    return Journey(api)


@pytest.mark.parametrize("settled_by", ["rail webhook", "reconciliation sweep"])
def test_an_allow_becomes_a_captured_payment_the_tenant_hears_about(
    journey: Journey, settled_by: str
) -> None:
    api = journey.api
    secret = journey.register().json()["secret"]

    decision = journey.allow()
    paid = journey.pay(decision["decision_id"])
    payment: dict[str, Any] = paid.json()
    assert paid.status_code == 202
    assert (payment["status"], payment["decision_id"]) == ("scheduled", decision["decision_id"])
    assert payment["amount"] == {"minor": 1_250_00, "currency": "INR"}  # the Decision's

    # The notice period: nothing is debited, however often the worker runs.
    api.clock.advance(NOTICE - timedelta(minutes=1))
    run_debit_step(api.store, api.rail, api.clock)
    assert journey.charges(payment["payment_id"]) == 0

    # Once it ends, the worker debits the card exactly once.
    api.clock.advance(timedelta(minutes=1))
    run_debit_step(api.store, api.rail, api.clock)
    run_debit_step(api.store, api.rail, api.clock)
    debited: dict[str, Any] = journey.payment(payment["payment_id"]).json()
    assert debited["status"] == "scheduled"
    assert debited["rail_payment_id"] is not None
    assert journey.charges(payment["payment_id"]) == 1

    # The rail says it captured: by its webhook, or the sweep asks it.
    api.rail.settle(journey.tenant_id(), PaymentId(payment["payment_id"]))
    if settled_by == "rail webhook":
        assert journey.deliver(event(debited), "evt_e2e").status_code == 200
    else:
        api.clock.advance(SWEEP_AGE)
        sweep(api.store, api.rail, api.clock, SWEEP_AGE)
    captured: dict[str, Any] = journey.payment(payment["payment_id"]).json()
    assert captured["status"] == "captured"
    assert captured["rail_payment_id"] == debited["rail_payment_id"]

    # The Tenant hears about it through its signed webhook.
    journey.relay()
    [(body, raw, headers)] = [r for r in journey.received() if r[0]["type"] == "payment.captured"]
    assert verifies(raw, headers["X-AgentPay-Signature"], secret)
    assert body["data"]["payment_id"] == payment["payment_id"]

    # And the evidence holds: the Decision and each Payment status are in the chain.
    chain = journey.chain()
    assert next(r.kind for r in chain if r.decision_id == decision["decision_id"]) == "decision"
    statuses = [
        r.payment.status
        for r in chain
        if r.payment is not None and r.payment.payment_id == payment["payment_id"]
    ]
    assert statuses[0] == "scheduled" and statuses[-1] == "captured"
    messages: list[str] = []
    assert verify_chain(api.store, journey.tenant_id(), messages.append) == 0, messages


# --- Demo journey 3: a disputed Payment -------------------------------------------------


@pytest.fixture
def dispute_journey(api: Api, tmp_path: Path) -> DisputeHooks:
    api.rail = MockRail(notice=NOTICE, clock=api.clock)
    # The Phase 1 decision rules, with default@1.4.0's Dispute rules.
    api.use_packs(release_test_pack(tmp_path, "1.0.1", {"disputes": DISPUTE_RULES}))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    return DisputeHooks(api)


def test_a_disputed_payment_gets_a_cited_signed_off_pack_that_verifies(
    dispute_journey: DisputeHooks,
) -> None:
    journey = dispute_journey
    api = journey.api

    # A captured Payment, the way journey 2 gets one.
    decision = journey.allow()
    payment: dict[str, Any] = journey.pay(decision["decision_id"]).json()
    api.clock.advance(NOTICE)
    run_debit_step(api.store, api.rail, api.clock)
    api.rail.settle(journey.tenant_id(), PaymentId(payment["payment_id"]))
    debited = journey.payment(payment["payment_id"]).json()
    assert journey.deliver(event(debited), "evt_journey_3").status_code == 200
    assert journey.status(payment) == "captured"

    # The Payer disputes it with their bank; the Tenant reports it.
    opened = journey.open(payment["payment_id"], reason="unauthorised")
    assert opened.status_code == 201, opened.text
    dispute_id: str = opened.json()["dispute_id"]
    assert opened.json()["status"] == "open"

    # The outbox starts the pack workflow, whose build activity runs the dispute evidence
    # agent: here a scripted model reply that cites the Audit trail.
    journey.relay()
    assert api.workflow.dispute_starts[dispute_id] == journey.tenant_id()
    ids = journey.trail(decision["decision_id"])
    claims = [
        ("mandate", "The Payer's Mandate allowed up to INR 10,000 a payment.", ids["decision"]),
        ("decision", "ShopBot asked to pay FreshMart INR 1,250 and was allowed.", ids["decision"]),
        ("payment", "The Payment was captured.", ids["payment"][-1:]),
        ("dispute", "The Payer disputed it as unauthorised.", ids["dispute_opened"]),
    ]
    reply = json.dumps({"claims": [{"kind": k, "text": t, "cites": c} for k, t, c in claims]})
    assert journey.build(dispute_id, reply) == "built"

    dispute: dict[str, Any] = journey.get(dispute_id).json()
    assert dispute["status"] == "in_review"
    pack = dispute["pack"]
    assert pack["narrative_source"] == "model"
    trail_ids = {r["audit_record_id"] for r in pack["bundle"]["trail"]}
    assert all(set(c["cites"]) <= trail_ids for c in pack["narrative"])
    assessment = pack["assessment"]
    assert (assessment["visa_code"], assessment["mastercard_code"]) == ("10.4", "4837")
    assert assessment["strength"] == "mixed"  # an allow with no Intent check

    # A reviewer opens the Review link, reads the pack and signs it off.
    api.clock.advance(timedelta(minutes=1))
    reviews = Reviews(journey)
    token = reviews.token(dispute_id)
    viewed = reviews.view(token).json()
    assert (viewed["reviewer"], viewed["dispute"]["pack_hash"]) == (REVIEWER, dispute["pack_hash"])
    signed = reviews.sign_off(token)
    assert signed.status_code == 200, signed.text
    ready: dict[str, Any] = journey.get(dispute_id).json()
    assert ready["status"] == "ready"
    assert ready["sign_off"]["pack_hash"] == dispute["pack_hash"]
    assert ready["sign_off"]["reviewer"] == REVIEWER

    # The Tenant hears both steps through its signed webhook.
    journey.relay()
    events = journey.dispute_events()
    assert [body["type"] for body, _, _ in events] == ["dispute.in_review", "dispute.ready"]
    assert all(verifies(raw, h["X-AgentPay-Signature"], journey.secret) for _, raw, h in events)

    # And anyone with the chain can check the signed pack: verify-pack passes.
    out: list[str] = []
    code = verify_pack_command(api.store, journey.tenant_id(), dispute_id, api.packs, out.append)
    assert code == IDENTICAL, out
    assert "Pack v1: matches its Sign-off." in out
