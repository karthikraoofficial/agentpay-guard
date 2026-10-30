"""POST /v1/decisions/{id}/void: release an unpaid allow, or approved step-up, from
Spent this month (#80).

A Void is an Audit record of kind `void` naming its Decision; the Decision never changes.
Spent this month leaves Voided Decisions out, and a Voided Decision can't be paid. Every
chain verifies and every Decision replays when each test ends."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from adapters.memory.rail import MockRail
from tests.api.conftest import Api
from tests.api.test_consent_approve import inr
from tests.api.test_payments import Payments, token
from tests.api.test_tenants_payers_agents import assert_problem
from tests.packs import phase_1_packs


class Voids(Payments):
    def void(self, decision_id: str, *, key: str | None = None, headers=None):  # type: ignore[no-untyped-def]
        path = f"/v1/decisions/{decision_id}/void"
        headers = headers or self.tenant
        if key is None:
            return self.api.post(path, headers)
        return self.api.client.post(path, headers={**headers, "Idempotency-Key": key})

    def tight(self, cap: int) -> str:
        """A Mandate whose Monthly cap and per-transaction cap are both `cap` rupees."""
        mandate = self.create(
            rail_token=token(MockRail.token_that_captures()),
            per_txn_cap=inr(cap * 100),
            monthly_cap=inr(cap * 100),
        )
        mandate_id: str = mandate["mandate_id"]
        return mandate_id


@pytest.fixture
def voids(api: Api, tmp_path: Path) -> Voids:
    api.use_packs(phase_1_packs(tmp_path))
    return Voids(api)


# --- Releasing spend ----------------------------------------------------------------


def test_voiding_the_allow_that_used_the_cap_lets_the_next_decision_allow(voids: Voids) -> None:
    mandate_id = voids.tight(2_000)
    first = voids.allow(mandate_id=mandate_id)
    over = voids.ask(mandate_id=mandate_id).json()
    assert (over["outcome"], over["reason_codes"][0]) == ("block", "MONTHLY_CAP_EXCEEDED")

    response = voids.void(first["decision_id"])

    assert response.status_code == 200
    void = response.json()
    assert void["decision_id"] == first["decision_id"]
    assert void["voided_at"] == "2026-11-02T09:30:00Z"
    assert void["audit_record_id"].startswith("aud_")
    assert voids.ask(mandate_id=mandate_id).json()["outcome"] == "allow"


def test_voiding_an_approved_step_up_releases_it(voids: Voids) -> None:
    mandate_id = voids.tight(10_000)
    step_up = voids.ask(mandate_id=mandate_id, amount=inr(6_000_00)).json()
    voids.approve(step_up["step_up"]["consent_request_id"])
    assert voids.ask(mandate_id=mandate_id, amount=inr(5_000_00)).json()["outcome"] == "block"

    voids.void(step_up["decision_id"])

    after = voids.ask(mandate_id=mandate_id, amount=inr(5_000_00)).json()
    assert (after["outcome"], after["reason_codes"]) == (
        "step_up",
        ["AMOUNT_ABOVE_STEP_UP_THRESHOLD"],
    )


def test_a_step_up_approved_next_month_is_released_from_that_month(voids: Voids) -> None:
    mandate_id = voids.tight(10_000)
    voids.api.clock.advance(timedelta(days=28, hours=14, minutes=25))  # 2026-11-30 23:55Z
    step_up = voids.ask(mandate_id=mandate_id, amount=inr(6_000_00)).json()
    voids.api.clock.advance(timedelta(minutes=7))  # approved 2026-12-01 00:02Z
    voids.approve(step_up["step_up"]["consent_request_id"])
    december = voids.allow(mandate_id=mandate_id, amount=inr(1_000_00))
    spent = voids.audit(december["audit_record_id"]).record.inputs.spent_this_month
    assert spent.minor == 6_000_00  # counted in December, the month it was approved

    voids.void(step_up["decision_id"])

    later = voids.allow(mandate_id=mandate_id, amount=inr(1_000_00))
    spent = voids.audit(later["audit_record_id"]).record.inputs.spent_this_month
    assert spent.minor == 1_000_00  # only the December allow is left


def test_an_allow_whose_window_closed_can_still_be_voided(voids: Voids) -> None:
    decision = voids.allow()
    voids.api.clock.advance(timedelta(hours=2))

    assert voids.void(decision["decision_id"]).status_code == 200


# --- Refusals -----------------------------------------------------------------------


def test_a_decision_with_a_payment_cannot_be_voided(voids: Voids) -> None:
    decision = voids.allow()
    voids.pay(decision["decision_id"])

    assert_problem(voids.void(decision["decision_id"]), 409, "DECISION_HAS_PAYMENT")


def test_a_second_void_with_a_new_key_is_refused(voids: Voids) -> None:
    decision = voids.allow()
    voids.void(decision["decision_id"])

    assert_problem(voids.void(decision["decision_id"]), 409, "DECISION_ALREADY_VOIDED")


def test_a_decision_that_was_never_payable_cannot_be_voided(voids: Voids) -> None:
    block = voids.ask(merchant={"id": "m_9", "name": "Casino", "mcc": "7995"}).json()
    pending = voids.step_up()
    declined = voids.step_up()
    voids.decline(declined["step_up"]["consent_request_id"])

    for decision in (block, pending, declined):
        assert_problem(voids.void(decision["decision_id"]), 409, "DECISION_NOT_PAYABLE")


def test_another_tenants_or_an_unknown_decision_is_not_found(voids: Voids) -> None:
    other = Voids(voids.api, "other")
    theirs = other.allow()

    assert_problem(voids.void(theirs["decision_id"]), 404, "DECISION_NOT_FOUND")
    assert_problem(voids.void("dec_unknown"), 404, "DECISION_NOT_FOUND")


def test_voiding_needs_a_manage_key_and_an_idempotency_key(voids: Voids) -> None:
    decision = voids.allow()

    assert_problem(
        voids.void(decision["decision_id"], headers=voids.decide), 403, "INSUFFICIENT_SCOPE"
    )
    missing = voids.api.client.post(
        f"/v1/decisions/{decision['decision_id']}/void", headers=voids.tenant
    )
    assert_problem(missing, 400, "IDEMPOTENCY_KEY_MISSING")


def test_a_retry_with_the_same_key_returns_the_same_void(voids: Voids) -> None:
    decision = voids.allow()
    first = voids.void(decision["decision_id"], key="void-1")

    again = voids.void(decision["decision_id"], key="void-1")

    assert again.content == first.content
    assert again.headers["Idempotent-Replayed"] == "true"


# --- A Voided Decision --------------------------------------------------------------


def test_a_voided_decision_cannot_be_paid_and_has_no_window(voids: Voids) -> None:
    decision = voids.allow()
    voids.void(decision["decision_id"])

    assert_problem(voids.pay(decision["decision_id"]), 409, "DECISION_NOT_PAYABLE")
    after = voids.decision(decision["decision_id"])
    assert after["payable_until"] is None
    assert after["outcome"] == "allow"  # the Decision itself never changes
    assert voids.api.rail.charges_made == 0


def test_the_void_joins_the_audit_chain_and_later_decisions_replay(voids: Voids) -> None:
    decision = voids.allow()

    void = voids.void(decision["decision_id"]).json()
    later = voids.allow()

    record = voids.audit(void["audit_record_id"]).record
    assert record.kind == "void"
    assert record.decision_id == decision["decision_id"]
    assert record.void.via == "tenant_api"
    assert record.void.payment_id is None
    assert record.inputs is None and record.output is None
    later_record = voids.audit(later["audit_record_id"]).record
    assert later_record.inputs.spent_this_month.minor == 0  # the Voided allow is left out


# --- A failed Payment Voids its Decision (#81) --------------------------------------


def test_a_failed_payment_voids_its_decision_and_a_tenant_void_is_then_refused(
    api: Api, tmp_path: Path
) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    voids = Voids(api, rail_token=MockRail.token_that_is_refused())
    decision = voids.allow()

    payment: dict[str, Any] = voids.pay(decision["decision_id"]).json()

    assert payment["status"] == "failed"
    record = voids.chain()[-1]
    assert (record.kind, record.void.via, record.void.payment_id) == (
        "void",
        "payment_failed",
        payment["payment_id"],
    )
    assert_problem(voids.void(decision["decision_id"]), 409, "DECISION_ALREADY_VOIDED")
    later = voids.allow()
    assert voids.audit(later["audit_record_id"]).record.inputs.spent_this_month.minor == 0
