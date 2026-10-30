"""POST /v1/rail/razorpay/webhooks: Razorpay tells us how each Payment ended (#81).

No API key: the signature over the raw body is the authentication. Events find their
Payment, and through it the Tenant, by our Payment id in the Razorpay payment's notes.
Status only moves forward, a failed Payment writes a Void, and only a few fields of each
event are kept. Bodies are signed here with a test secret; no real Razorpay is needed."""

import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from adapters.memory.rail import MockRail
from tests.api.conftest import Api
from tests.api.test_payments import Payments
from tests.api.test_tenants_payers_agents import assert_problem
from tests.conftest import make_settings
from tests.packs import phase_1_packs

SECRET = "whsec_test_do_not_print"  # noqa: S105 - a test-only signing secret
PATH = "/v1/rail/razorpay/webhooks"


def event(payment: dict[str, Any], kind: str = "payment.captured", **entity: Any) -> dict[str, Any]:
    """A Razorpay webhook body for `payment`, shaped like Razorpay's own samples."""
    failed = kind == "payment.failed"
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": kind,
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment["rail_payment_id"] or "pay_unknown",
                    "entity": "payment",
                    "amount": payment["amount"]["minor"],
                    "currency": payment["amount"]["currency"],
                    "status": "failed" if failed else "captured",
                    "order_id": "order_test",
                    "method": "card",
                    "notes": {"agentpay_payment_id": payment["payment_id"]},
                    "email": "payer@example.test",
                    "contact": "+919900000000",
                    "error_code": "BAD_REQUEST_ERROR" if failed else None,
                    "error_reason": "payment_failed" if failed else None,
                }
                | entity
            }
        },
        "created_at": 1793000000,
    }


class Webhooks(Payments):
    def deliver(self, body: dict[str, Any], event_id: str = "evt_1", *, secret: str = SECRET):  # type: ignore[no-untyped-def]
        raw = json.dumps(body).encode()
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return self.api.client.post(
            PATH,
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
                "X-Razorpay-Event-Id": event_id,
            },
        )

    def paid(self) -> dict[str, Any]:
        payment: dict[str, Any] = self.pay(self.allow()["decision_id"]).json()
        assert payment["status"] == "scheduled"
        return payment

    def status(self, payment: dict[str, Any]) -> str:
        current: str = self.payment(payment["payment_id"]).json()["status"]
        return current

    def payment_records(self) -> list[Any]:
        return [r for r in self.chain() if r.kind == "payment"]

    def stored_event(self, event_id: str):  # type: ignore[no-untyped-def]
        with self.api.store.transaction(self.tenant_id()) as tx:
            return tx.get_rail_event(event_id)


@pytest.fixture
def hooks(api: Api, tmp_path: Path) -> Webhooks:
    api.use_packs(phase_1_packs(tmp_path))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    return Webhooks(api)


# --- Settling ------------------------------------------------------------------------


def test_a_captured_event_captures_the_payment(hooks: Webhooks) -> None:
    payment = hooks.paid()

    response = hooks.deliver(event(payment))

    assert response.status_code == 200
    assert hooks.status(payment) == "captured"
    record = hooks.payment_records()[-1]
    assert (record.payment.status, record.payment.rail_payment_id) == (
        "captured",
        payment["rail_payment_id"],
    )


def test_a_failed_event_fails_the_payment_and_voids_its_decision(hooks: Webhooks) -> None:
    decision = hooks.allow()
    payment = hooks.pay(decision["decision_id"]).json()

    hooks.deliver(event(payment, "payment.failed"))

    current = hooks.payment(payment["payment_id"]).json()
    assert (current["status"], current["failure_code"]) == ("failed", "BAD_REQUEST_ERROR")
    void = hooks.chain()[-1]
    assert (void.kind, void.decision_id) == ("void", decision["decision_id"])
    assert (void.void.via, void.void.payment_id) == ("payment_failed", payment["payment_id"])
    later = hooks.allow()
    assert hooks.audit(later["audit_record_id"]).record.inputs.spent_this_month.minor == 0


def test_the_webhook_needs_no_api_key(hooks: Webhooks) -> None:
    payment = hooks.paid()

    response = hooks.deliver(event(payment))  # deliver() sends no Authorization header

    assert response.status_code == 200


# --- Signatures ----------------------------------------------------------------------


def test_a_bad_or_missing_signature_is_refused_and_logged_without_the_body(
    hooks: Webhooks, caplog: pytest.LogCaptureFixture
) -> None:
    payment = hooks.paid()
    body = event(payment)

    with caplog.at_level(logging.INFO):
        forged = hooks.deliver(body, secret="whsec_wrong")  # noqa: S106 - a wrong test secret
        unsigned = hooks.api.client.post(
            PATH, content=json.dumps(body).encode(), headers={"X-Razorpay-Event-Id": "evt_1"}
        )

    assert_problem(forged, 400, "BAD_SIGNATURE")
    assert_problem(unsigned, 400, "BAD_SIGNATURE")
    assert hooks.status(payment) == "scheduled"
    assert hooks.stored_event("evt_1") is None
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "signature" in logged
    for secret_or_body in (payment["payment_id"], "payer@example.test", "whsec_"):
        assert secret_or_body not in logged


def test_without_a_webhook_secret_every_event_is_refused(api: Api, tmp_path: Path) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    hooks = Webhooks(api)  # default settings: no AGENTPAY_RAZORPAY_WEBHOOK_SECRET
    payment = hooks.paid()

    assert_problem(hooks.deliver(event(payment)), 400, "BAD_SIGNATURE")
    assert hooks.status(payment) == "scheduled"


def test_an_event_without_an_event_id_is_refused(hooks: Webhooks) -> None:
    payment = hooks.paid()

    assert_problem(hooks.deliver(event(payment), event_id=""), 400, "EVENT_ID_MISSING")
    assert hooks.status(payment) == "scheduled"


# --- Duplicates and order ------------------------------------------------------------


def test_the_same_event_twice_changes_things_once(hooks: Webhooks) -> None:
    payment = hooks.paid()

    first = hooks.deliver(event(payment, "payment.failed"), "evt_dup")
    again = hooks.deliver(event(payment, "payment.failed"), "evt_dup")

    assert (first.status_code, again.status_code) == (200, 200)
    assert [r.payment.status for r in hooks.payment_records()] == ["scheduled", "failed"]
    assert len([r for r in hooks.chain() if r.kind == "void"]) == 1


def test_a_failed_event_after_captured_changes_nothing(hooks: Webhooks) -> None:
    payment = hooks.paid()
    hooks.deliver(event(payment), "evt_captured")

    late = hooks.deliver(event(payment, "payment.failed"), "evt_failed")

    assert late.status_code == 200
    assert hooks.status(payment) == "captured"
    assert [r.kind for r in hooks.chain()].count("void") == 0
    assert [r.payment.status for r in hooks.payment_records()] == ["scheduled", "captured"]


# --- Dropped events ------------------------------------------------------------------


def test_an_unknown_event_type_or_payment_is_dropped_with_a_200(hooks: Webhooks) -> None:
    payment = hooks.paid()
    head = len(hooks.chain())
    stranger = payment | {"payment_id": "pmt_unknown"}
    unnoted = event(payment, notes=[])  # Razorpay sends [] for empty notes

    for body, event_id in (
        (event(payment, "refund.created"), "evt_refund"),
        (event(stranger), "evt_stranger"),
        (unnoted, "evt_unnoted"),
    ):
        assert hooks.deliver(body, event_id).status_code == 200
        assert hooks.stored_event(event_id) is None

    assert hooks.status(payment) == "scheduled"
    assert len(hooks.chain()) == head


def test_an_event_for_a_different_amount_is_dropped(hooks: Webhooks) -> None:
    payment = hooks.paid()

    response = hooks.deliver(event(payment, amount=payment["amount"]["minor"] + 1), "evt_odd")

    assert response.status_code == 200
    assert hooks.status(payment) == "scheduled"
    assert hooks.stored_event("evt_odd") is None


def test_an_event_for_another_razorpay_payment_is_dropped(hooks: Webhooks) -> None:
    payment = hooks.paid()

    stale = event(payment, "payment.failed", id="pay_an_earlier_attempt")
    response = hooks.deliver(stale, "evt_stale")

    assert response.status_code == 200
    assert hooks.status(payment) == "scheduled"
    assert hooks.stored_event("evt_stale") is None
    assert [r.kind for r in hooks.chain()].count("void") == 0


def test_a_capture_for_a_payment_whose_rail_call_timed_out_is_applied(
    api: Api, tmp_path: Path
) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    hooks = Webhooks(api, rail_token=MockRail.token_that_times_out())
    payment = hooks.pay(hooks.allow()["decision_id"]).json()
    assert payment["rail_payment_id"] is None

    hooks.deliver(event(payment | {"rail_payment_id": "pay_from_razorpay"}), "evt_late")

    current = hooks.payment(payment["payment_id"]).json()
    assert (current["status"], current["rail_payment_id"]) == ("captured", "pay_from_razorpay")


def test_a_capture_after_a_payment_failed_changes_nothing_but_is_logged_loudly(
    api: Api, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    hooks = Webhooks(api, rail_token=MockRail.token_that_is_refused())
    payment = hooks.pay(hooks.allow()["decision_id"]).json()
    assert payment["status"] == "failed"

    with caplog.at_level(logging.ERROR):
        hooks.deliver(event(payment | {"rail_payment_id": "pay_surprise"}), "evt_surprise")

    assert hooks.status(payment) == "failed"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("captured at the rail after it failed" in r.getMessage() for r in errors)


# --- What is kept --------------------------------------------------------------------


def test_a_stored_event_keeps_only_the_allowed_fields(hooks: Webhooks) -> None:
    payment = hooks.paid()

    hooks.deliver(event(payment, "payment.failed"), "evt_kept")

    stored = hooks.stored_event("evt_kept")
    assert stored is not None
    assert stored.model_dump(mode="json") == {
        "tenant_id": hooks.tenant_id(),
        "received_at": "2026-11-02T09:30:00Z",
        "event_id": "evt_kept",
        "event_type": "payment.failed",
        "payment_id": payment["payment_id"],
        "rail_payment_id": payment["rail_payment_id"],
        "status": "failed",
        "failure_code": "BAD_REQUEST_ERROR",
    }
    assert "payer@example.test" not in repr(stored)


# --- A Payment the rail refused at once ----------------------------------------------


def test_a_payment_the_rail_refuses_at_once_also_voids_its_decision(
    api: Api, tmp_path: Path
) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    payments = Payments(api, rail_token=MockRail.token_that_is_refused())
    decision = payments.allow()

    payment = payments.pay(decision["decision_id"]).json()

    assert payment["status"] == "failed"
    void = payments.chain()[-1]
    assert (void.kind, void.void.via, void.void.payment_id) == (
        "void",
        "payment_failed",
        payment["payment_id"],
    )
