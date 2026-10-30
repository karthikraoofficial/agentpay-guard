import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from adapters.postgres.store import PostgresStore
from api.main import create_app
from tests.api.conftest import NOW, Api, MovableClock
from tests.api.test_decisions import Pay
from tests.conftest import make_settings
from tests.packs import phase_1_packs, release_test_pack


def inr(minor: int) -> dict[str, Any]:
    return {"minor": minor, "currency": "INR"}


@pytest.fixture
def pay(api: Api, tmp_path: Path) -> Pay:
    # Caps and Spent this month are Phase 1 rules: tested on default@1.0.0 so medium
    # amounts don't also need an Intent check. Their edges under 1.1.0 are in the Phase 2
    # golden set.
    api.use_packs(phase_1_packs(tmp_path))
    return Pay(api)


def spend(pay: Pay, minor: int, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = pay.ask(amount=inr(minor), **overrides).json()
    return body


# --- Spent this month ---------------------------------------------------------------


def test_only_allowed_decisions_count_towards_the_monthly_cap(pay: Pay) -> None:
    # Mandate: per-transaction INR 10,000, monthly INR 50,000; cap-near at INR 40,000.
    assert spend(pay, 4_000_00)["outcome"] == "allow"
    assert spend(pay, 9_000_00)["outcome"] == "step_up"  # not counted
    assert (
        spend(pay, 4_000_00, merchant={"id": "x", "name": "X", "mcc": "7995"})["outcome"] == "block"
    )
    for _ in range(8):
        assert spend(pay, 4_000_00)["outcome"] == "allow"  # 9 x 4,000 = 36,000 spent

    assert spend(pay, 4_000_00)["reason_codes"] == ["MONTHLY_CAP_NEAR"]  # 40,000 would be 80%


def test_the_monthly_total_resets_at_the_utc_month_boundary(api: Api, tmp_path: Path) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    api.clock.at = datetime(2026, 11, 30, 23, 59, tzinfo=UTC)
    pay = Pay(api)
    for _ in range(9):
        assert spend(pay, 4_000_00)["outcome"] == "allow"
    assert spend(pay, 4_000_00)["reason_codes"] == ["MONTHLY_CAP_NEAR"]

    api.clock.advance(timedelta(minutes=1))  # 2026-12-01T00:00Z

    assert spend(pay, 4_000_00)["outcome"] == "allow"


def test_the_audit_record_holds_the_spend_the_engine_saw(pay: Pay) -> None:
    spend(pay, 3_000_00)
    second = spend(pay, 2_000_00)

    assert pay.audit(second["audit_record_id"]).record.inputs.spent_this_month.minor == 3_000_00


# --- step_up and Consent requests ----------------------------------------------------


def test_a_step_up_creates_a_pending_consent_request(pay: Pay) -> None:
    body = spend(pay, 6_000_00)

    assert body["outcome"] == "step_up"
    assert body["reason_codes"] == ["AMOUNT_ABOVE_STEP_UP_THRESHOLD"]
    step_up = body["step_up"]
    assert step_up["consent_request_id"].startswith("csr_")
    assert step_up["expires_at"] == "2026-11-02T09:40:00Z"
    assert step_up["status_url"] == f"/v1/decisions/{body['decision_id']}"
    assert body["consent"] == {
        "status": "pending",
        "resolved_at": None,
        "reason_codes": None,
        "mandate_inactive": False,
    }
    fetched = pay.api.client.get(step_up["status_url"], headers=pay.tenant).json()
    assert fetched == body


def test_a_consent_request_reads_as_expired_after_ten_minutes(pay: Pay) -> None:
    url = spend(pay, 6_000_00)["step_up"]["status_url"]

    pay.api.clock.advance(timedelta(minutes=9, seconds=59))
    assert pay.api.client.get(url, headers=pay.tenant).json()["consent"]["status"] == "pending"
    pay.api.clock.advance(timedelta(seconds=1))
    assert pay.api.client.get(url, headers=pay.tenant).json()["consent"]["status"] == "expired"


def test_a_consent_request_expires_after_the_active_packs_consent_ttl(
    api: Api, tmp_path: Path
) -> None:
    api.use_packs(release_test_pack(tmp_path, "1.0.1", {"consent_ttl_seconds": 120}))
    pay = Pay(api)

    body = spend(pay, 6_000_00)

    assert body["step_up"]["expires_at"] == "2026-11-02T09:32:00Z"  # Decision time + 120 s
    pay.api.clock.advance(timedelta(seconds=120))
    url = body["step_up"]["status_url"]
    assert pay.api.client.get(url, headers=pay.tenant).json()["consent"]["status"] == "expired"


def test_allow_and_block_have_no_step_up_or_consent(pay: Pay) -> None:
    for body in (spend(pay, 100_00), spend(pay, 20_000_00)):
        assert (body["step_up"], body["consent"]) == (None, None)


def test_no_threshold_or_cap_amount_is_ever_returned(pay: Pay) -> None:
    for minor in (6_000_00, 20_000_00):
        text = pay.ask(amount=inr(minor)).text
        for secret in ("500000", "1000000", "5000000", "8000", "200000"):
            assert secret not in text


# --- Concurrency (Postgres only) ---------------------------------------------------------


def test_parallel_requests_cannot_overspend_the_monthly_cap(migrated_database_url: str) -> None:
    store = PostgresStore(create_engine(migrated_database_url, pool_size=12))
    clock = MovableClock(NOW)
    pay = Pay(Api(TestClient(create_app(make_settings(), store=store, clock=clock)), store, clock))
    # Monthly INR 5,000 and per-transaction INR 1,000: three allows (3,000) then the
    # fourth would reach the 80% mark (4,000) and steps up, which doesn't count.
    mandate_id = pay.create(per_txn_cap=inr(1_000_00), monthly_cap=inr(5_000_00))["mandate_id"]
    start = threading.Barrier(10)
    outcomes: list[str] = []
    lock = threading.Lock()

    def ask() -> None:
        start.wait()
        outcome = pay.ask(mandate_id=mandate_id, amount=inr(1_000_00)).json()["outcome"]
        with lock:
            outcomes.append(outcome)

    workers = [threading.Thread(target=ask) for _ in range(10)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)

    assert len(outcomes) == 10
    assert outcomes.count("allow") == 3
    assert outcomes.count("step_up") == 7
