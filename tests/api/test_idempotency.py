import threading
from datetime import datetime, timedelta

from tests.api.conftest import Api
from tests.api.test_tenants_payers_agents import assert_problem

ASHA = {"display_name": "Asha", "contact_ref": "tok_1"}


def _post(api: Api, path: str, tenant: dict[str, str], key: str, json: object | None = None):  # type: ignore[no-untyped-def]
    return api.client.post(path, headers={**tenant, "Idempotency-Key": key}, json=json)


def test_a_repeated_write_returns_the_stored_response_byte_for_byte(api: Api) -> None:
    tenant = api.new_tenant()

    first = _post(api, "/v1/payers", tenant, "k-repeat", ASHA)
    again = _post(api, "/v1/payers", tenant, "k-repeat", ASHA)

    assert first.status_code == again.status_code == 201
    assert again.content == first.content
    assert again.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers


def test_a_repeated_disable_is_replayed_not_reapplied(api: Api) -> None:
    tenant = api.new_tenant()
    agent_id = api.post("/v1/agents", tenant, {"name": "ShopBot"}).json()["agent_id"]
    disable = f"/v1/agents/{agent_id}/disable"

    first = _post(api, disable, tenant, "k-disable")
    api.post(f"/v1/agents/{agent_id}/enable", tenant)
    again = _post(api, disable, tenant, "k-disable")

    assert again.content == first.content
    assert again.headers["Idempotent-Replayed"] == "true"
    assert api.client.get(f"/v1/agents/{agent_id}", headers=tenant).json()["status"] == "active"


def test_reusing_a_key_with_a_different_body_is_a_conflict(api: Api) -> None:
    tenant = api.new_tenant()
    _post(api, "/v1/payers", tenant, "k-reused", ASHA)

    response = _post(api, "/v1/payers", tenant, "k-reused", {**ASHA, "display_name": "Ravi"})

    assert_problem(response, 409, "IDEMPOTENCY_KEY_REUSED")


def test_reusing_a_key_on_a_different_endpoint_is_a_conflict(api: Api) -> None:
    tenant = api.new_tenant()
    _post(api, "/v1/payers", tenant, "k-other-path", ASHA)

    response = _post(api, "/v1/agents", tenant, "k-other-path", {"name": "ShopBot"})

    assert_problem(response, 409, "IDEMPOTENCY_KEY_REUSED")


class _BlockingClock:
    """Holds the first request inside its transaction until released."""

    def __init__(self, at: datetime) -> None:
        self.at = at
        self.entered = threading.Event()
        self.release = threading.Event()

    def now(self) -> datetime:
        if not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(timeout=10)
        return self.at


def test_a_key_still_in_flight_is_refused_with_retry_after(api: Api) -> None:
    tenant = api.new_tenant()
    clock = _BlockingClock(api.clock.now())
    slow = api.client_with(clock)
    results: dict[str, int] = {}

    def first_call() -> None:
        r = slow.post("/v1/payers", headers={**tenant, "Idempotency-Key": "k-flight"}, json=ASHA)
        results["first"] = r.status_code

    worker = threading.Thread(target=first_call)
    worker.start()
    assert clock.entered.wait(timeout=10)
    try:
        second = _post(api, "/v1/payers", tenant, "k-flight", ASHA)
    finally:
        clock.release.set()
        worker.join(timeout=10)

    assert_problem(second, 409, "IDEMPOTENCY_IN_PROGRESS")
    assert second.headers["Retry-After"] == "1"
    assert results["first"] == 201
    assert (
        _post(api, "/v1/payers", tenant, "k-flight", ASHA).headers["Idempotent-Replayed"] == "true"
    )


class _FailingOnceClock:
    def __init__(self, at: datetime) -> None:
        self.at = at
        self.failed = False

    def now(self) -> datetime:
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated crash mid-write")
        return self.at


def test_a_write_that_failed_with_500_runs_fresh_on_retry(api: Api) -> None:
    tenant = api.new_tenant()
    flaky = api.client_with(_FailingOnceClock(api.clock.now()), raise_server_exceptions=False)
    headers = {**tenant, "Idempotency-Key": "k-crash"}

    crashed = flaky.post("/v1/payers", headers=headers, json=ASHA)
    retried = flaky.post("/v1/payers", headers=headers, json=ASHA)

    assert_problem(crashed, 500, "INTERNAL")
    assert retried.status_code == 201
    assert "Idempotent-Replayed" not in retried.headers


def test_the_same_key_in_two_tenants_never_collides(api: Api) -> None:
    a, b = api.new_tenant("a"), api.new_tenant("b")

    from_a = _post(api, "/v1/payers", a, "k-shared", ASHA)
    from_b = _post(api, "/v1/payers", b, "k-shared", {**ASHA, "display_name": "Ravi"})

    assert from_a.status_code == from_b.status_code == 201
    assert from_b.json()["display_name"] == "Ravi"
    assert "Idempotent-Replayed" not in from_b.headers


def test_a_key_is_kept_for_24_hours_then_treated_as_new(api: Api) -> None:
    tenant = api.new_tenant()
    first = _post(api, "/v1/payers", tenant, "k-ttl", ASHA)

    api.clock.advance(timedelta(hours=23, minutes=59))
    within = _post(api, "/v1/payers", tenant, "k-ttl", ASHA)
    api.clock.advance(timedelta(minutes=1))
    after = _post(api, "/v1/payers", tenant, "k-ttl", ASHA)

    assert within.content == first.content
    assert after.status_code == 201
    assert "Idempotent-Replayed" not in after.headers
    assert after.json()["payer_id"] != first.json()["payer_id"]
