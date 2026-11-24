"""Demo control switches (#281, ADR-0032): forced failures for the Demo Tenant on the mock
rail. A switch runs before any Decision, so its Audit record names the Mandate instead.
Every switch is scoped to the run's Mandate, except the rail outage."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from adapters.memory.settling_rail import SettlingMockRail
from core.demo_switches import switchable_writer
from core.domain.evidence import EvidenceBundle
from core.domain.parties import TenantId
from core.evidence import ModelNarrative
from core.reconcile import sweep
from tests.api.conftest import Api
from tests.api.test_demo_controls import SMALL, Demo, _grant
from tests.conftest import make_settings

ORDER = {"minor": 6_000_00, "currency": "INR"}
CART = {
    "payer_instruction": "Order snacks for the party, up to INR 6,000.",
    "merchant": {"id": "m_freshmart", "name": "FreshMart", "mcc": "5411"},
    "line_items": [{"description": "Party snacks", "quantity": 1, "unit_amount": ORDER}],
    "quoted_total": ORDER,
}


@pytest.fixture
def demo(api: Api) -> Demo:
    headers = api.new_tenant("Demo")
    tenant_id = api.tenant_of(headers)
    api.use_settings(make_settings(mock_settle_after_seconds=60, demo_controls_tenant_id=tenant_id))
    mandate_id, agent_id = _grant(api, headers)
    return Demo(api, headers, tenant_id, mandate_id, agent_id)


def _intent(demo: Demo, mandate_id: str, agent_id: str) -> dict[str, Any]:
    created = demo.api.post(
        "/v1/intents", demo.headers, {**CART, "mandate_id": mandate_id, "agent_id": agent_id}
    )
    assert created.status_code == 201, created.text
    read = demo.api.client.get(f"/v1/intents/{created.json()['intent_id']}", headers=demo.headers)
    body: dict[str, Any] = read.json()
    return body


def _kinds(api: Api, tenant_id: TenantId) -> list[str]:
    with api.store.transaction(tenant_id) as tx:
        rows, _ = tx.read_audit_chain()
    return [row.kind for row in rows]


def test_ai_off_fails_the_intent_check_closed_for_that_mandate_only(demo: Demo) -> None:
    other_mandate, other_agent = _grant(demo.api, demo.headers)

    switched = demo.control(f"mandates/{demo.mandate_id}/ai-off")

    assert switched.status_code == 200, switched.text
    body = switched.json()
    assert (body["control"], body["target_id"]) == ("ai_off", demo.mandate_id)
    assert body["decision_id"] is None
    off = _intent(demo, demo.mandate_id, demo.agent_id)
    assert (off["verdict"], off["failure_cause"]) == ("unknown", "disabled")
    assert _intent(demo, other_mandate, other_agent)["verdict"] == "match"
    assert _kinds(demo.api, demo.tenant_id) == ["demo_control"]
    verified = demo.api.client.get("/v1/audit-chain/verify", headers=demo.headers).json()
    assert verified == {"clean": True, "record_count": 1, "first_broken_seq": None}
    read = demo.api.client.get(
        f"/v1/audit-records/{body['audit_record_id']}", headers=demo.headers
    ).json()
    assert (read["kind"], read["decision_id"]) == ("demo_control", None)


def test_a_switch_needs_a_real_mandate(demo: Demo) -> None:
    missing = demo.control("mandates/man_nope/ai-off")

    assert missing.status_code == 404


def test_another_tenant_cant_use_a_switch(demo: Demo) -> None:
    other = demo.api.new_tenant("other")

    for path in ("ai-off", "rail-declining", "lose-payment"):
        assert demo.control(f"mandates/{demo.mandate_id}/{path}", other).status_code == 404
    assert demo.control("rail-down", other).status_code == 404


def _pay(demo: Demo, decision: dict[str, Any]) -> Any:
    return demo.api.post("/v1/payments", demo.headers, {"decision_id": decision["decision_id"]})


def test_rail_declining_fails_charges_for_that_mandate_only(demo: Demo) -> None:
    other_mandate, other_agent = _grant(demo.api, demo.headers)
    other = Demo(demo.api, demo.headers, demo.tenant_id, other_mandate, other_agent)
    assert demo.control(f"mandates/{demo.mandate_id}/rail-declining").status_code == 200

    declined = _pay(demo, demo.decide(SMALL))
    fine = _pay(other, other.decide(SMALL))

    assert (declined.json()["status"], declined.json()["failure_code"]) == (
        "failed",
        "BAD_REQUEST_ERROR",
    )
    assert fine.json()["status"] == "scheduled"
    assert demo.api.rail.charges_for(demo.tenant_id, declined.json()["payment_id"]) == 0


def test_rail_declining_fails_a_refund(demo: Demo) -> None:
    payment = _pay(demo, demo.decide(SMALL)).json()
    demo.api.rail.settle(demo.tenant_id, payment["payment_id"])
    demo.api.clock.advance(timedelta(minutes=2))
    sweep(demo.api.store, demo.api.rail, demo.api.clock, timedelta(minutes=1))
    assert demo.control(f"mandates/{demo.mandate_id}/rail-declining").status_code == 200

    refund = demo.api.post(
        f"/v1/payments/{payment['payment_id']}/refunds", demo.headers, {"amount": SMALL}
    )

    assert refund.status_code == 201, refund.text
    assert refund.json()["status"] == "failed"
    assert demo.api.rail.refunds_for(demo.tenant_id, payment["payment_id"]) == 0


def test_a_lost_payment_is_failed_by_reconciliation(demo: Demo) -> None:
    assert demo.control(f"mandates/{demo.mandate_id}/lose-payment").status_code == 200

    payment = _pay(demo, demo.decide(SMALL)).json()
    demo.api.clock.advance(timedelta(minutes=2))
    sweep(demo.api.store, SettlingMockRail(demo.api.store), demo.api.clock, timedelta(minutes=1))

    read = demo.api.client.get(f"/v1/payments/{payment['payment_id']}", headers=demo.headers)
    assert (read.json()["status"], read.json()["failure_code"]) == (
        "failed",
        "RAIL_CHARGE_MISSING",
    )
    assert demo.api.rail.bookings_for(demo.tenant_id, payment["payment_id"]) == 0


def test_rail_down_refuses_payments_but_decisions_keep_working(demo: Demo) -> None:
    assert demo.control("rail-down").status_code == 200

    decided = demo.decide(SMALL)
    refused = _pay(demo, decided)

    assert decided["outcome"] == "allow"
    assert refused.status_code == 503
    assert refused.json()["code"] == "RAIL_UNAVAILABLE"
    assert int(refused.headers["Retry-After"]) > 0


def test_the_status_endpoint_is_public_and_reports_an_outage(demo: Demo) -> None:
    assert demo.api.client.get("/v1/demo/status").json()["rail_outage"] is False

    demo.control("rail-down")

    during = demo.api.client.get("/v1/demo/status").json()
    assert during["rail_outage"] is True
    assert during["outage_ends_at"] is not None
    demo.api.clock.advance(timedelta(seconds=61))
    assert demo.api.client.get("/v1/demo/status").json()["rail_outage"] is False


def test_the_outage_ends_and_payments_flow_again(demo: Demo) -> None:
    demo.control("rail-down")
    decided = demo.decide(SMALL)
    demo.api.clock.advance(timedelta(seconds=61))

    paid = _pay(demo, decided)

    assert paid.status_code == 202, paid.text
    assert paid.json()["status"] == "scheduled"


def test_rail_down_has_a_cooldown(demo: Demo) -> None:
    first = demo.control("rail-down").json()

    joined = demo.control("rail-down").json()
    demo.api.clock.advance(timedelta(seconds=61))
    cooling = demo.control("rail-down")
    demo.api.clock.advance(timedelta(seconds=240))
    again = demo.control("rail-down")

    assert joined["control_id"] == first["control_id"]  # one outage, one record
    assert cooling.status_code == 409
    assert cooling.json()["code"] == "RAIL_DOWN_COOLDOWN"
    assert int(cooling.headers["Retry-After"]) == 239
    assert again.status_code == 200
    assert again.json()["control_id"] != first["control_id"]


def test_a_tampered_copy_is_caught_and_the_database_is_unchanged(demo: Demo) -> None:
    first = demo.decide(SMALL)
    demo.decide(SMALL)
    second_seq = demo.api.client.get(
        f"/v1/audit-records/{first['audit_record_id']}", headers=demo.headers
    ).json()["seq"]

    caught = demo.api.client.get(
        f"/v1/demo/audit-records/{first['audit_record_id']}/tampered-copy", headers=demo.headers
    )

    assert caught.status_code == 200, caught.text
    assert caught.json() == {
        "clean": False,
        "record_count": 2,
        "first_broken_seq": second_seq,
        "tampered_seq": second_seq,
    }
    real = demo.api.client.get("/v1/audit-chain/verify", headers=demo.headers).json()
    assert real["clean"] is True


def test_a_tampered_copy_needs_the_demo_tenant_and_manage(demo: Demo) -> None:
    audit_id = demo.decide(SMALL)["audit_record_id"]
    path = f"/v1/demo/audit-records/{audit_id}/tampered-copy"
    narrow = demo.api.new_key(demo.tenant_id, frozenset({"decide"}))

    assert demo.api.client.get(path, headers=narrow).status_code == 403
    assert demo.api.client.get(path, headers=demo.api.new_tenant("x")).status_code == 404


def test_ai_off_gives_a_template_narrative(demo: Demo) -> None:
    calls: list[str] = []

    def model(tenant_id: TenantId, bundle: EvidenceBundle) -> ModelNarrative:
        calls.append(bundle.dispute_id)
        return ModelNarrative(None, ())

    writer = switchable_writer(model, demo.api.store, demo.tenant_id)
    bundle = SimpleNamespace(
        dispute_id="dsp_1", mandate=SimpleNamespace(mandate_id=demo.mandate_id)
    )

    writer(demo.tenant_id, bundle)  # type: ignore[arg-type]
    assert calls == ["dsp_1"]
    demo.control(f"mandates/{demo.mandate_id}/ai-off")
    written = writer(demo.tenant_id, bundle)  # type: ignore[arg-type]

    assert calls == ["dsp_1"]  # no model call
    assert written.claims is None
