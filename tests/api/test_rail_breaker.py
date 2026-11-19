"""The circuit breaker on rail calls (#226, ADR-0025): after a run of unknown outcomes from
the rail, payments and refunds fail fast with 503 RAIL_UNAVAILABLE and Retry-After, write
nothing, and leave the Idempotency-Key free. Decisions never depend on the rail."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from adapters.memory.rail import MockRail
from core.ports import RailUnavailable
from tests.api.conftest import Api
from tests.api.test_disputes import DISPUTE_RULES, SECRET
from tests.api.test_payments import Payments, token
from tests.api.test_refunds import Refunds, inr
from tests.api.test_tenants_payers_agents import assert_problem
from tests.conftest import make_settings
from tests.packs import phase_1_packs, release_test_pack

FAILURES, OPEN_SECONDS = 3, 30


class Outage(Payments):
    """A Tenant whose default Mandate's card times out at the rail, and one that works."""

    def __init__(self, api: Api) -> None:
        super().__init__(api, rail_token=MockRail.token_that_times_out())
        self.working = self.create(rail_token=token(MockRail.token_that_captures()))["mandate_id"]

    def timeouts(self, n: int) -> list[dict[str, Any]]:
        """n Payments whose rail call times out: each is accepted, and stays scheduled."""
        made = []
        for _ in range(n):
            paid = self.pay(self.allow()["decision_id"])
            assert paid.status_code == 202, paid.text
            made.append(paid.json())
        return made


@pytest.fixture
def outage(api: Api, tmp_path: Path) -> Outage:
    api.use_packs(phase_1_packs(tmp_path))
    api.use_settings(
        make_settings(rail_breaker_failures=FAILURES, rail_breaker_open_seconds=OPEN_SECONDS)
    )
    return Outage(api)


def test_after_a_run_of_rail_timeouts_payments_fail_fast_and_write_nothing(
    outage: Outage,
) -> None:
    outage.timeouts(FAILURES)
    decision = outage.allow(mandate_id=outage.working)
    charges = outage.api.rail.charges_made

    refused = outage.pay(decision["decision_id"])

    assert_problem(refused, 503, "RAIL_UNAVAILABLE")
    assert refused.headers["Retry-After"] == str(OPEN_SECONDS)
    assert outage.api.rail.charges_made == charges  # the rail was never asked
    assert [
        r
        for r in outage.chain()
        if r.kind == "payment" and r.decision_id == decision["decision_id"]
    ] == []


def test_a_decision_refused_by_the_breaker_is_paid_once_the_rail_answers(
    outage: Outage,
) -> None:
    outage.timeouts(FAILURES)
    decision = outage.allow(mandate_id=outage.working)
    assert outage.pay(decision["decision_id"], key="pay-1").status_code == 503

    outage.api.clock.advance(timedelta(seconds=OPEN_SECONDS))
    trial = outage.pay(decision["decision_id"], key="pay-1")  # the same key: it was left free

    assert trial.status_code == 202, trial.text
    assert trial.json()["status"] == "scheduled"
    assert "Idempotent-Replayed" not in trial.headers
    # The trial worked, so the breaker closed: the next Payment goes straight through.
    assert outage.pay(outage.allow(mandate_id=outage.working)["decision_id"]).status_code == 202


def test_a_failed_trial_opens_the_breaker_again(outage: Outage) -> None:
    outage.timeouts(FAILURES)
    outage.api.clock.advance(timedelta(seconds=OPEN_SECONDS))

    [trial] = outage.timeouts(1)  # the one request let through times out too

    assert trial["status"] == "scheduled"
    assert_problem(outage.pay(outage.allow()["decision_id"]), 503, "RAIL_UNAVAILABLE")


def test_rail_refusals_are_answers_and_never_open_the_breaker(api: Api, tmp_path: Path) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    api.use_settings(make_settings(rail_breaker_failures=FAILURES))
    refused_card = Payments(api, rail_token=MockRail.token_that_is_refused())

    for _ in range(FAILURES + 1):
        paid = refused_card.pay(refused_card.allow()["decision_id"])
        assert (paid.status_code, paid.json()["status"]) == (202, "failed")


def test_decisions_are_unaffected_while_the_breaker_is_open(outage: Outage) -> None:
    outage.timeouts(FAILURES)

    decided = outage.ask()

    assert decided.status_code == 200
    assert decided.json()["outcome"] == "allow"


def test_refunds_fail_fast_too_and_write_no_refund(
    api: Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.use_packs(release_test_pack(tmp_path, "1.0.1", {"disputes": DISPUTE_RULES}))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET, rail_breaker_failures=FAILURES))
    refunds = Refunds(api)
    payment = refunds.captured()

    def times_out(*args: Any) -> None:
        raise RailUnavailable("timed out")

    monkeypatch.setattr(api.rail, "refund", times_out)
    pending = [refunds.refund(payment["payment_id"], amount=inr(1_000)) for _ in range(FAILURES)]
    refused = refunds.refund(payment["payment_id"], amount=inr(1_000))

    assert [p.json()["status"] for p in pending] == ["pending"] * FAILURES
    assert_problem(refused, 503, "RAIL_UNAVAILABLE")
    shown = refunds.payment(payment["payment_id"], headers=refunds.manage).json()
    assert len(shown["refunds"]) == FAILURES  # the refused one wrote nothing


def test_an_answer_resets_the_run_so_scattered_timeouts_never_open_it(outage: Outage) -> None:
    for _ in range(FAILURES):  # FAILURES - 1 timeouts, then an answer, each round
        outage.timeouts(FAILURES - 1)
        worked = outage.pay(outage.allow(mandate_id=outage.working)["decision_id"])
        assert worked.status_code == 202, worked.text

    assert outage.pay(outage.allow(mandate_id=outage.working)["decision_id"]).status_code == 202
