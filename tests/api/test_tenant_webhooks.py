"""Signed Tenant webhooks: a Tenant registers one HTTPS endpoint, and the outbox relay
sends it consent and payment events, signed with a secret shown once (ADR-0015)."""

import hashlib
import hmac
import json
import logging
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from adapters.memory.clock import FixedClock
from adapters.memory.rail import MockRail
from adapters.memory.unit_of_work import InMemoryUnitOfWork
from adapters.memory.workflow import InMemoryWorkflow
from adapters.webhooks.secret_box import secret_box
from api.admin import create_tenant
from core.charges import apply_charge
from core.domain.payments import PaymentId
from core.domain.webhooks import WebhookEndpoint
from core.ports import DomainEvent
from core.reconcile import sweep
from core.relay import relay_once
from core.tenant_webhooks import TenantWebhooks
from tests.api.conftest import NOW, Api, MovableClock, RecordingSender, tenant_webhooks
from tests.api.test_payments import Payments
from tests.api.test_tenants_payers_agents import assert_problem
from tests.conftest import make_settings
from tests.packs import phase_1_packs

EVERYTHING = 100_000  # the Postgres outbox is shared by every test in the run


class Hooks(Payments):
    """A Tenant with a mock Rail token, and the relay sending to a recording endpoint."""

    def __init__(self, api: Api, rail_token: Any = None) -> None:
        super().__init__(api, rail_token=rail_token)
        self.url = f"https://hooks.example/{uuid.uuid4().hex}"
        self.sender = RecordingSender()

    def register(self, url: str | None = None, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        return self.api.post(
            "/v1/webhook-endpoint", headers or self.tenant, {"url": url or self.url}
        )

    def rotate(self):  # type: ignore[no-untyped-def]
        return self.api.post("/v1/webhook-endpoint/rotate-secret", self.tenant)

    def relay(self) -> None:
        webhooks = tenant_webhooks(self.api.store, self.api.clock, self.sender)
        relay_once(self.api.store, self.api.workflow, self.api.clock, webhooks, limit=EVERYTHING)

    def received(self) -> list[tuple[dict[str, Any], bytes, dict[str, str]]]:
        """What this Tenant's endpoint got: (parsed body, raw body, headers)."""
        return [(json.loads(b), b, h) for url, b, h in self.sender.sent if url == self.url]

    def types(self) -> list[str]:
        return [body["type"] for body, _, _ in self.received()]


def verifies(raw: bytes, header: str, secret: str) -> bool:
    """Check a signature the way a Tenant would, from the documented scheme alone."""
    parts = dict(part.split("=", 1) for part in header.split(","))
    expected = hmac.new(secret.encode(), f"{parts['t']}.".encode() + raw, hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest(), parts["v1"])


@pytest.fixture
def hooks(api: Api, tmp_path: Path) -> Hooks:
    api.use_packs(phase_1_packs(tmp_path))
    return Hooks(api)


# --- Registration ---------------------------------------------------------------------


def test_registering_shows_the_secret_once_and_stores_it_encrypted(hooks: Hooks) -> None:
    made = hooks.register()

    assert made.status_code == 201
    secret = made.json()["secret"]
    assert secret.startswith("whsec_")
    read = hooks.api.client.get("/v1/webhook-endpoint", headers=hooks.tenant)
    assert read.status_code == 200
    assert read.json()["url"] == hooks.url
    assert read.json()["secret"] is None
    with hooks.api.store.transaction(hooks.tenant_id()) as tx:
        stored = tx.get_webhook_endpoint()
    assert stored is not None
    assert secret not in stored.secret_ciphertext


def test_a_replayed_registration_never_shows_the_secret_again(hooks: Hooks) -> None:
    headers = {**hooks.tenant, "Idempotency-Key": "register-1"}
    body = {"url": hooks.url}
    first = hooks.api.client.post("/v1/webhook-endpoint", headers=headers, json=body)

    again = hooks.api.client.post("/v1/webhook-endpoint", headers=headers, json=body)

    assert first.json()["secret"]
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["secret"] is None


def test_registering_again_moves_the_endpoint_and_keeps_the_secret(hooks: Hooks) -> None:
    secret = hooks.register().json()["secret"]
    hooks.url = f"https://hooks.example/{uuid.uuid4().hex}"

    moved = hooks.register()
    hooks.step_up()
    hooks.relay()

    assert moved.status_code == 200
    assert moved.json()["secret"] is None
    [(_, raw, headers)] = hooks.received()
    assert verifies(raw, headers["X-AgentPay-Signature"], secret)


@pytest.mark.parametrize(
    "url", ["http://hooks.example/x", "hooks.example", "https://", "ftp://hooks.example"]
)
def test_only_an_https_url_is_accepted(hooks: Hooks, url: str) -> None:
    assert_problem(hooks.register(url), 422, "WEBHOOK_URL_INVALID")


def test_an_agent_key_cannot_register_or_read_the_endpoint(hooks: Hooks) -> None:
    assert hooks.register(headers=hooks.decide).status_code == 403
    assert hooks.api.client.get("/v1/webhook-endpoint", headers=hooks.decide).status_code == 403


def test_without_an_endpoint_reading_or_rotating_is_not_found(hooks: Hooks) -> None:
    missing = hooks.api.client.get("/v1/webhook-endpoint", headers=hooks.tenant)

    assert_problem(missing, 404, "WEBHOOK_ENDPOINT_NOT_FOUND")
    assert_problem(hooks.rotate(), 404, "WEBHOOK_ENDPOINT_NOT_FOUND")


# --- The four events --------------------------------------------------------------------


def test_each_event_reaches_the_endpoint_signed_with_the_shown_secret(hooks: Hooks) -> None:
    secret = hooks.register().json()["secret"]
    csr = hooks.step_up()["step_up"]["consent_request_id"]
    hooks.approve(csr)
    captured = hooks.pay(hooks.allow()["decision_id"]).json()
    hooks.api.rail.settle(hooks.tenant_id(), PaymentId(captured["payment_id"]))
    hooks.api.clock.advance(timedelta(minutes=11))
    sweep(hooks.api.store, hooks.api.rail, hooks.api.clock, timedelta(minutes=10))

    hooks.relay()

    assert sorted(hooks.types()) == [
        "consent_request.created",
        "consent_request.resolved",
        "payment.captured",
    ]
    for body, raw, headers in hooks.received():
        assert verifies(raw, headers["X-AgentPay-Signature"], secret)
        assert headers["X-AgentPay-Event-Id"] == body["id"]
        assert body["id"].startswith("evt_")
    by_type = {body["type"]: body["data"] for body, _, _ in hooks.received()}
    assert by_type["consent_request.created"]["consent_request_id"] == csr
    assert by_type["consent_request.resolved"] | {"resolved_at": None} == {
        "consent_request_id": csr,
        "decision_id": by_type["consent_request.created"]["decision_id"],
        "status": "approved",
        "resolved_via": "tenant_api",
        "resolved_at": None,
    }
    assert by_type["payment.captured"] == {
        "payment_id": captured["payment_id"],
        "decision_id": captured["decision_id"],
        "mandate_id": captured["mandate_id"],
        "status": "captured",
        "amount_minor": 1_250_00,
        "currency": "INR",
        "failure_code": None,
    }


def test_a_failed_payment_sends_payment_failed_with_its_code(api: Api, tmp_path: Path) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    hooks = Hooks(api, MockRail.token_that_is_refused())
    secret = hooks.register().json()["secret"]

    payment = hooks.pay(hooks.allow()["decision_id"]).json()
    hooks.relay()

    [(body, raw, headers)] = hooks.received()
    assert verifies(raw, headers["X-AgentPay-Signature"], secret)
    assert body["type"] == "payment.failed"
    assert body["data"]["payment_id"] == payment["payment_id"]
    assert body["data"]["failure_code"] == "BAD_REQUEST_ERROR"


def test_a_rolled_back_change_sends_no_event(hooks: Hooks) -> None:
    hooks.register()
    payment = hooks.pay(hooks.allow()["decision_id"]).json()
    hooks.api.rail.settle(hooks.tenant_id(), PaymentId(payment["payment_id"]))
    charge = hooks.api.rail.get_charge(hooks.tenant_id(), PaymentId(payment["payment_id"]))

    with pytest.raises(RuntimeError, match="after the change"):
        with hooks.api.store.transaction(hooks.tenant_id()) as tx:
            apply_charge(tx, PaymentId(payment["payment_id"]), charge, hooks.api.clock.now())
            raise RuntimeError("the transaction failed after the change")
    hooks.relay()

    assert hooks.types() == []
    assert hooks.payment(payment["payment_id"]).json()["status"] == "scheduled"


def test_bodies_hold_no_payer_contact_rail_token_refs_or_free_text(hooks: Hooks) -> None:
    hooks.register()
    csr = hooks.step_up()["step_up"]["consent_request_id"]
    hooks.approve(csr)
    payment = hooks.pay(hooks.allow()["decision_id"]).json()
    hooks.api.rail.settle(hooks.tenant_id(), PaymentId(payment["payment_id"]))
    hooks.api.clock.advance(timedelta(minutes=11))
    sweep(hooks.api.store, hooks.api.rail, hooks.api.clock, timedelta(minutes=10))

    hooks.relay()

    allowed = {
        "consent_request_id",
        "decision_id",
        "mandate_id",
        "payment_id",
        "status",
        "expires_at",
        "resolved_at",
        "resolved_via",
        "amount_minor",
        "currency",
        "failure_code",
    }
    assert len(hooks.received()) == 3
    for body, raw, _ in hooks.received():
        assert set(body) == {"id", "type", "created_at", "data"}
        assert set(body["data"]) <= allowed
        for leak in (b"token_mock", b"cust_mock", b"visa", b"merchant", b"@", b"attestation"):
            assert leak not in raw


# --- Delivery -----------------------------------------------------------------------------


def test_a_tenant_without_an_endpoint_gets_nothing_sent(hooks: Hooks) -> None:
    hooks.step_up()

    hooks.relay()
    hooks.relay()

    assert hooks.sender.sent == [] or all(url != hooks.url for url, _, _ in hooks.sender.sent)
    tenant = hooks.tenant_id()
    waiting = [e for e in hooks.api.store.undelivered(EVERYTHING) if e.tenant_id == tenant]
    assert waiting == []


@pytest.mark.parametrize("failure", ["status", "unreachable"])
def test_a_failing_endpoint_is_retried_with_backoff_then_given_up_after_a_day(
    hooks: Hooks, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)
    hooks.register()
    if failure == "status":
        hooks.sender.status = 500
    else:
        hooks.sender.unreachable = True
    hooks.step_up()
    clock = hooks.api.clock

    hooks.relay()  # first try fails: next in 30 s
    clock.advance(timedelta(seconds=29))
    hooks.relay()
    assert len(hooks.received()) == 1
    clock.advance(timedelta(seconds=1))
    hooks.relay()  # second try: next in 60 s
    assert len(hooks.received()) == 2
    clock.advance(timedelta(seconds=59))
    hooks.relay()
    assert len(hooks.received()) == 2
    clock.advance(timedelta(seconds=1))
    hooks.relay()
    assert len(hooks.received()) == 3

    tries = 3
    while clock.now() < NOW + timedelta(hours=24):
        clock.advance(timedelta(hours=1))  # the wait never grows past an hour
        hooks.relay()
        tries += 1
        assert len(hooks.received()) == tries
    clock.advance(timedelta(hours=1))
    hooks.relay()
    assert len(hooks.received()) == tries  # given up: never sent again

    assert any("gave up on Tenant webhook" in r.getMessage() for r in caplog.records)
    tenant = hooks.tenant_id()
    assert [e for e in hooks.api.store.undelivered(EVERYTHING) if e.tenant_id == tenant] == []


def test_a_delivered_event_is_not_sent_again(hooks: Hooks) -> None:
    hooks.register()
    hooks.step_up()

    hooks.relay()
    hooks.relay()

    assert hooks.types() == ["consent_request.created"]


# --- Rotation -----------------------------------------------------------------------------


def test_rotating_signs_later_events_with_the_new_secret(hooks: Hooks) -> None:
    old = hooks.register().json()["secret"]
    hooks.step_up()
    hooks.relay()

    rotated = hooks.rotate()
    hooks.step_up()
    hooks.relay()

    assert rotated.status_code == 200
    new = rotated.json()["secret"]
    assert new != old
    first, second = hooks.received()
    assert verifies(first[1], first[2]["X-AgentPay-Signature"], old)
    assert verifies(second[1], second[2]["X-AgentPay-Signature"], new)
    assert not verifies(second[1], second[2]["X-AgentPay-Signature"], old)
    read = hooks.api.client.get("/v1/webhook-endpoint", headers=hooks.tenant).json()
    assert read["secret"] is None
    assert read["secret_rotated_at"] == rotated.json()["secret_rotated_at"]


# --- The relay's batches and deadline, on a store of their own --------------------------


def _outbox_with_endpoint() -> tuple[InMemoryUnitOfWork, MovableClock, RecordingSender]:
    store, clock, sender = InMemoryUnitOfWork(), MovableClock(NOW), RecordingSender()
    create_tenant(store, "solo", live=False, clock=FixedClock(NOW))
    [tenant] = list(store._tenants)
    box = secret_box(make_settings())
    with store.transaction(tenant) as tx:
        tx.save_webhook_endpoint(
            WebhookEndpoint(
                tenant_id=tenant,
                url="https://hooks.example/solo",
                secret_ciphertext=box.encrypt("whsec_solo"),
                created_at=NOW,
                secret_rotated_at=NOW,
            )
        )
    return store, clock, sender


def _publish(
    store: InMemoryUnitOfWork, event_type: str, n: int, at: Any, attempts: int = 0
) -> None:
    [tenant] = list(store._tenants)
    with store.transaction(tenant) as tx:
        for i in range(n):
            tx.publish(
                DomainEvent(
                    event_id=f"evt_{event_type}_{i}",
                    tenant_id=tenant,
                    event_type=event_type,  # type: ignore[arg-type]
                    occurred_at=at,
                    attempts=attempts,
                    payload={
                        "consent_request_id": f"csr_{i}",
                        "expires_at": "2026-11-02T00:00:00Z",
                    },
                )
            )


def test_stuck_workflow_events_never_hold_up_tenant_webhooks() -> None:
    store, clock, sender = _outbox_with_endpoint()
    _publish(store, "consent_requested", 5, NOW - timedelta(minutes=1))  # older, so first
    _publish(store, "consent_request.created", 1, NOW)
    workflow = InMemoryWorkflow()
    workflow.fail_starts = True

    relay_once(
        store,
        workflow,
        clock,
        TenantWebhooks(store, secret_box(make_settings()), sender, clock),
        limit=2,
    )

    assert [url for url, _, _ in sender.sent] == ["https://hooks.example/solo"]


def test_retries_stop_exactly_a_day_after_the_event() -> None:
    store, clock, sender = _outbox_with_endpoint()
    sender.status = 503
    _publish(store, "consent_request.created", 1, NOW, attempts=8)  # its next wait: an hour
    webhooks = TenantWebhooks(store, secret_box(make_settings()), sender, clock)
    clock.advance(timedelta(hours=23, minutes=30))

    relay_once(store, InMemoryWorkflow(), clock, webhooks)  # fails: next try capped at 24 h
    clock.advance(timedelta(minutes=30))
    relay_once(store, InMemoryWorkflow(), clock, webhooks)  # the last try, then given up

    assert len(sender.sent) == 2
    assert store.undelivered(10) == []
