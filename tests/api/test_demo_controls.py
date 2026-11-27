"""Demo controls (#280, ADR-0032): the Demo Tenant's timers can be ended early, on the mock
rail only. Each control writes its own Audit record, edits nothing, moves no clock, and
every Decision before and after still replays byte for byte. Any other Tenant's deadlines
ignore the controls table."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from adapters.memory.clock import FixedClock
from adapters.memory.rail import MockRail
from adapters.memory.settling_rail import SettlingMockRail
from api.admin import create_tenant
from api.payability import payable_until
from core.debits import run_debit_step
from core.disputes import expire_dispute
from core.domain.decisions import DecisionId
from core.domain.demo_controls import DemoControl
from core.domain.disputes import DisputeId
from core.domain.parties import TenantId
from core.evidence import build_evidence_pack
from core.pack_verify import verify_pack
from core.policy.packs import load_active_pack
from core.reconcile import sweep
from tests.api.conftest import NOW, Api
from tests.conftest import make_settings

SMALL = {"minor": 1_250_00, "currency": "INR"}
LARGE = {"minor": 6_000_00, "currency": "INR"}


def _iso(delta: timedelta) -> str:
    return (NOW + delta).isoformat().replace("+00:00", "Z")


@dataclass
class Demo:
    api: Api
    headers: dict[str, str]
    tenant_id: TenantId
    mandate_id: str
    agent_id: str

    def decide(self, amount: dict[str, Any]) -> dict[str, Any]:
        body = {
            "mandate_id": self.mandate_id,
            "agent_id": self.agent_id,
            "amount": amount,
            "merchant": {"id": "m_freshmart", "name": "FreshMart", "mcc": "5411"},
        }
        response = self.api.post("/v1/decisions", self.headers, body)
        assert response.status_code == 200, response.text
        decided: dict[str, Any] = response.json()
        return decided

    def control(self, path: str, headers: dict[str, str] | None = None) -> Any:
        return self.api.post(f"/v1/demo/{path}", headers or self.headers)


def _grant(api: Api, headers: dict[str, str], card: str = "token_mock_captures") -> tuple[str, str]:
    payer = api.post("/v1/payers", headers, {"display_name": "Asha", "contact_ref": "c"}).json()
    agent = api.post("/v1/agents", headers, {"name": "ShopBot"}).json()
    mandate = api.post(
        "/v1/mandates",
        headers,
        {
            "payer_id": payer["payer_id"],
            "allowed_agent_ids": [agent["agent_id"]],
            "currency": "INR",
            "per_txn_cap": {"minor": 10_000_00, "currency": "INR"},
            "monthly_cap": {"minor": 50_000_00, "currency": "INR"},
            "allowed_mccs": ["5411"],
            "valid_from": _iso(-timedelta(minutes=5)),
            "expires_at": _iso(timedelta(days=30)),
            "consent_evidence": {
                "method": "in_app_confirm",
                "reference": "r",
                "captured_at": _iso(-timedelta(minutes=6)),
            },
            "rail_token": {
                "rail": "mock",
                "customer_ref": "cust_mock",
                "token_ref": card,
                "display": "visa 1111",
            },
        },
    ).json()
    return mandate["mandate_id"], agent["agent_id"]


@pytest.fixture
def demo(api: Api) -> Demo:
    headers = api.new_tenant("Demo")
    tenant_id = api.tenant_of(headers)
    api.use_settings(make_settings(mock_settle_after_seconds=60, demo_controls_tenant_id=tenant_id))
    mandate_id, agent_id = _grant(api, headers)
    return Demo(api, headers, tenant_id, mandate_id, agent_id)


def test_expire_now_ends_a_consent_request_as_the_timer_would(demo: Demo) -> None:
    csr = demo.decide(LARGE)["step_up"]["consent_request_id"]

    ended = demo.control(f"consent-requests/{csr}/expire")

    assert ended.status_code == 200, ended.text
    assert ended.json()["control"] == "expire_consent"
    read = demo.api.client.get(f"/v1/consent-requests/{csr}", headers=demo.headers).json()
    assert read["status"] == "expired"
    with demo.api.store.transaction(demo.tenant_id) as tx:
        rows, _ = tx.read_audit_chain()
    kinds = [row.kind for row in rows][-2:]
    assert kinds == ["demo_control", "consent_resolution"]
    demo.api.assert_evidence_holds()


def test_a_resolved_consent_request_cant_be_expired(demo: Demo) -> None:
    csr = demo.decide(LARGE)["step_up"]["consent_request_id"]
    demo.control(f"consent-requests/{csr}/expire")

    again = demo.control(f"consent-requests/{csr}/expire")

    assert again.status_code == 409
    assert again.json()["code"] == "CONSENT_ALREADY_RESOLVED"


def test_expire_now_closes_a_payment_window(demo: Demo) -> None:
    decision = demo.decide(SMALL)
    path = f"/v1/decisions/{decision['decision_id']}"
    open_until = demo.api.client.get(path, headers=demo.headers).json()["payable_until"]

    ended = demo.control(f"decisions/{decision['decision_id']}/expire-payment-window")
    pay = demo.api.post("/v1/payments", demo.headers, {"decision_id": decision["decision_id"]})

    assert ended.status_code == 200, ended.text
    closed_at = demo.api.client.get(path, headers=demo.headers).json()["payable_until"]
    assert closed_at < open_until
    assert pay.status_code == 409
    assert pay.json()["code"] == "PAYMENT_WINDOW_CLOSED"
    # Decisions made before and after the control still replay byte for byte.
    demo.decide(SMALL)
    demo.api.assert_evidence_holds()


def test_a_decision_that_was_never_payable_has_no_window_to_close(demo: Demo) -> None:
    decision = demo.decide(LARGE)  # a step_up nobody approved

    ended = demo.control(f"decisions/{decision['decision_id']}/expire-payment-window")

    assert ended.status_code == 409
    assert ended.json()["code"] == "DECISION_NOT_PAYABLE"


def test_controls_need_the_manage_scope(demo: Demo) -> None:
    decide_only = demo.api.new_key(demo.tenant_id, {"decide"})
    decision = demo.decide(SMALL)

    refused = demo.control(
        f"decisions/{decision['decision_id']}/expire-payment-window", decide_only
    )

    assert refused.status_code == 403
    assert refused.json()["code"] == "INSUFFICIENT_SCOPE"


def test_any_other_tenant_gets_404(demo: Demo) -> None:
    other = demo.api.new_tenant("Other")
    mandate_id, agent_id = _grant(demo.api, other)
    theirs = Demo(demo.api, other, demo.api.tenant_of(other), mandate_id, agent_id).decide(SMALL)

    refused = demo.control(f"decisions/{theirs['decision_id']}/expire-payment-window", other)

    assert refused.status_code == 404


def test_without_a_demo_tenant_the_controls_dont_exist(api: Api) -> None:
    headers = api.new_tenant("Demo")

    missing = api.post("/v1/demo/decisions/dec_x/expire-payment-window", headers)

    assert missing.status_code == 404


def test_a_non_demo_tenants_deadlines_ignore_the_controls_table(api: Api) -> None:
    """Even a control row stored for another Tenant is never read: the gate is the
    configured Demo Tenant, not what the table holds (ADR-0032)."""
    headers = api.new_tenant("Acme")
    tenant_id = api.tenant_of(headers)
    demo_id, _, _ = create_tenant(api.store, "Demo", live=False, clock=FixedClock(NOW))
    api.use_settings(make_settings(demo_controls_tenant_id=demo_id))
    mandate_id, agent_id = _grant(api, headers)
    decision = Demo(api, headers, tenant_id, mandate_id, agent_id).decide(SMALL)
    pack = load_active_pack(api.packs, "default")
    with api.store.transaction(tenant_id) as tx:
        tx.add_demo_control(
            DemoControl(
                control_id="dctl_planted",
                tenant_id=tenant_id,
                kind="expire_payment_window",
                target_id=decision["decision_id"],
                decision_id=DecisionId(decision["decision_id"]),
                at=NOW,
                audit_record_id="aud_planted",  # type: ignore[arg-type]
            )
        )
    with api.store.transaction(tenant_id) as tx:
        stored = tx.get_decision(DecisionId(decision["decision_id"]))
        assert stored is not None
        until = payable_until(tx, stored, None, pack, api.packs, demo_tenant=demo_id)

    assert until is not None and until > NOW  # the planted control was ignored
    pay = api.post("/v1/payments", headers, {"decision_id": decision["decision_id"]})
    assert pay.status_code == 202, pay.text  # accepted: the window is still open


NOTICE = timedelta(minutes=10)


def _noticed(api: Api, card: str = "token_mock_captures") -> Demo:
    """The hosted demo's card-like debit: the API's mock rail books each charge with a
    notice period, and the worker's debit step debits it through the settling rail."""
    headers = api.new_tenant("Demo")
    tenant_id = api.tenant_of(headers)
    api.rail = MockRail(
        notice=NOTICE, clock=api.clock, debits=SettlingMockRail(api.store).get_charge
    )
    api.use_settings(
        make_settings(
            mock_settle_after_seconds=60,
            mock_notice_seconds=int(NOTICE.total_seconds()),
            demo_controls_tenant_id=tenant_id,
        )
    )
    mandate_id, agent_id = _grant(api, headers, card)
    return Demo(api, headers, tenant_id, mandate_id, agent_id)


def _pay(demo: Demo) -> dict[str, Any]:
    decision = demo.decide(SMALL)
    paid = demo.api.post("/v1/payments", demo.headers, {"decision_id": decision["decision_id"]})
    assert paid.status_code == 202, paid.text
    payment: dict[str, Any] = paid.json()
    assert payment["debit_after"] is not None and payment["rail_payment_id"] is None
    return payment


def _debit_step(demo: Demo, demo_tenant: TenantId | None = None) -> None:
    run_debit_step(
        demo.api.store,
        SettlingMockRail(demo.api.store),
        demo.api.clock,
        demo_tenant=demo.tenant_id if demo_tenant is None else demo_tenant,
    )


def _read(demo: Demo, payment_id: str) -> dict[str, Any]:
    found: dict[str, Any] = demo.api.client.get(
        f"/v1/payments/{payment_id}", headers=demo.headers
    ).json()
    return found


def test_without_a_control_the_notice_period_runs_its_course(api: Api) -> None:
    demo = _noticed(api)
    payment = _pay(demo)

    _debit_step(demo)

    assert _read(demo, payment["payment_id"])["rail_payment_id"] is None


def test_expire_now_ends_the_notice_period_and_the_debit_step_debits(api: Api) -> None:
    demo = _noticed(api)
    payment = _pay(demo)

    ended = demo.control(f"payments/{payment['payment_id']}/expire-notice")
    _debit_step(demo)

    assert ended.status_code == 200, ended.text
    debited = _read(demo, payment["payment_id"])
    assert debited["rail_payment_id"] is not None
    assert debited["debit_after"] == payment["debit_after"]  # the Payment wasn't edited
    demo.api.assert_evidence_holds()


def test_a_mandate_revoked_in_the_notice_period_stops_the_debit(api: Api) -> None:
    demo = _noticed(api)
    payment = _pay(demo)
    revoked = demo.api.post(
        f"/v1/mandates/{demo.mandate_id}/revoke", demo.headers, {"revoked_by": "payer"}
    )
    assert revoked.status_code == 200, revoked.text

    demo.control(f"payments/{payment['payment_id']}/expire-notice")
    _debit_step(demo)

    failed = _read(demo, payment["payment_id"])
    assert (failed["status"], failed["failure_code"]) == ("failed", "MANDATE_REVOKED")
    demo.api.assert_evidence_holds()


def test_a_notice_that_fails_is_never_debited(api: Api) -> None:
    demo = _noticed(api, card="token_mock_notice_fails")
    payment = _pay(demo)

    demo.control(f"payments/{payment['payment_id']}/expire-notice")
    _debit_step(demo)

    failed = _read(demo, payment["payment_id"])
    assert (failed["status"], failed["failure_code"]) == ("failed", "RAIL_NOTICE_FAILED")


def test_the_debit_step_ignores_controls_for_any_tenant_but_the_demo_one(api: Api) -> None:
    demo = _noticed(api)
    payment = _pay(demo)
    demo.control(f"payments/{payment['payment_id']}/expire-notice")

    _debit_step(demo, demo_tenant=TenantId("ten_someone_else"))

    assert _read(demo, payment["payment_id"])["rail_payment_id"] is None


def test_a_disputed_payment_whose_notice_was_ended_still_builds_its_evidence(api: Api) -> None:
    """Evidence bundles leave Demo control records out for now (ADR-0032): the pack is
    built from the Decision's other records, and the control stays in the Audit chain."""
    demo = _noticed(api)
    payment = _pay(demo)
    demo.control(f"payments/{payment['payment_id']}/expire-notice")
    _debit_step(demo)
    demo.api.clock.advance(NOTICE + timedelta(minutes=1))
    sweep(demo.api.store, SettlingMockRail(demo.api.store), demo.api.clock, timedelta(seconds=60))
    assert _read(demo, payment["payment_id"])["status"] == "captured"
    opened = demo.api.post(
        "/v1/disputes",
        demo.headers,
        {
            "payment_id": payment["payment_id"],
            "reason": "unauthorised",
            "respond_by": (demo.api.clock.now() + timedelta(days=10))
            .isoformat()
            .replace("+00:00", "Z"),
        },
    )
    assert opened.status_code == 201, opened.text

    built = build_evidence_pack(
        demo.api.store,
        demo.tenant_id,
        DisputeId(opened.json()["dispute_id"]),
        demo.api.clock,
        demo.api.packs,
        None,
    )

    assert built == "built"
    report = verify_pack(
        demo.api.store, demo.tenant_id, DisputeId(opened.json()["dispute_id"]), demo.api.packs
    )
    assert report is not None and [v.problems for v in report.versions] == [[]]
    demo.api.assert_evidence_holds()


def _disputed(demo: Demo) -> dict[str, Any]:
    """A captured INR 1,250 Payment, disputed with a 10-day deadline, its pack built."""
    decision = demo.decide(SMALL)
    paid = demo.api.post("/v1/payments", demo.headers, {"decision_id": decision["decision_id"]})
    assert paid.status_code == 202, paid.text
    demo.api.clock.advance(timedelta(minutes=2))
    sweep(demo.api.store, SettlingMockRail(demo.api.store), demo.api.clock, timedelta(seconds=60))
    respond_by = demo.api.clock.now() + timedelta(days=10)
    opened = demo.api.post(
        "/v1/disputes",
        demo.headers,
        {
            "payment_id": paid.json()["payment_id"],
            "reason": "unauthorised",
            "respond_by": respond_by.isoformat().replace("+00:00", "Z"),
        },
    )
    assert opened.status_code == 201, opened.text
    dispute: dict[str, Any] = opened.json()
    built = build_evidence_pack(
        demo.api.store,
        demo.tenant_id,
        DisputeId(dispute["dispute_id"]),
        demo.api.clock,
        demo.api.packs,
        None,
    )
    assert built == "built"
    return dispute


def _dispute(demo: Demo, dispute_id: str) -> dict[str, Any]:
    found: dict[str, Any] = demo.api.client.get(
        f"/v1/disputes/{dispute_id}", headers=demo.headers
    ).json()
    return found


def test_expire_now_expires_a_dispute_as_its_deadline_would(demo: Demo) -> None:
    dispute = _disputed(demo)

    ended = demo.control(f"disputes/{dispute['dispute_id']}/expire")

    assert ended.status_code == 200, ended.text
    assert ended.json()["control"] == "expire_dispute"
    assert ended.json()["decision_id"] == dispute["decision_id"]
    read = _dispute(demo, dispute["dispute_id"])
    assert read["status"] == "expired"
    assert read["respond_by"] == dispute["respond_by"]  # the deadline wasn't edited
    with demo.api.store.transaction(demo.tenant_id) as tx:
        rows, _ = tx.read_audit_chain()
    assert rows[-1].kind == "demo_control"
    events = {e.event_type for e in demo.api.store.undelivered(100)}
    # The Tenant hears of it as of a deadline, and the workflow stops waiting.
    assert {"dispute.expired", "dispute_closed"} <= events
    demo.api.assert_evidence_holds()


def test_the_deadline_timer_then_finds_the_dispute_closed(demo: Demo) -> None:
    dispute = _disputed(demo)
    demo.control(f"disputes/{dispute['dispute_id']}/expire")
    demo.api.clock.advance(timedelta(days=11))

    outcome = expire_dispute(
        demo.api.store, demo.tenant_id, DisputeId(dispute["dispute_id"]), demo.api.clock
    )

    assert outcome == "already_closed"


def test_a_closed_dispute_cant_be_expired(demo: Demo) -> None:
    dispute = _disputed(demo)
    withdrawn = demo.api.post(f"/v1/disputes/{dispute['dispute_id']}/withdraw", demo.headers)
    assert withdrawn.status_code == 200, withdrawn.text

    refused = demo.control(f"disputes/{dispute['dispute_id']}/expire")

    assert refused.status_code == 409
    assert refused.json()["code"] == "DISPUTE_CLOSED"


def test_another_tenants_dispute_cant_be_expired(demo: Demo) -> None:
    refused = demo.control("disputes/dsp_missing/expire")
    other = demo.api.new_tenant("Other")

    elsewhere = demo.control("disputes/dsp_missing/expire", other)

    assert (refused.status_code, elsewhere.status_code) == (404, 404)


def test_a_payment_the_worker_debited_can_be_refunded_through_the_api(api: Api) -> None:
    """With the notice period on, the worker makes the mock debit, so the API's mock rail
    learns of it from the stored Payment when asked for a refund."""
    demo = _noticed(api)
    payment = _pay(demo)
    demo.control(f"payments/{payment['payment_id']}/expire-notice")
    _debit_step(demo)
    demo.api.clock.advance(NOTICE + timedelta(minutes=1))
    sweep(demo.api.store, SettlingMockRail(demo.api.store), demo.api.clock, timedelta(seconds=60))

    refund = demo.api.post(
        f"/v1/payments/{payment['payment_id']}/refunds",
        demo.headers,
        {"amount": {"minor": 500_00, "currency": "INR"}, "reason": "Part came back"},
    )

    assert refund.status_code in (200, 201, 202), refund.text
    assert refund.json()["status"] == "processed"
