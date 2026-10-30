"""GET /v1/consent-requests: the Tenant finds the Consent requests to put in front of
its Payers. Listing needs a `consent` key; reading one works with any key."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.api.conftest import Api
from tests.api.test_decisions import Pay
from tests.api.test_tenants_payers_agents import assert_problem
from tests.packs import phase_1_packs

STEP_UP = {"minor": 6_000_00, "currency": "INR"}  # above default@1.0.0's step-up threshold


class Consents(Pay):
    def __init__(self, api: Api, name: str = "acme") -> None:
        super().__init__(api, name)
        self.consent = api.new_key(api.tenant_of(self.tenant), {"consent"})
        self.decide = api.new_key(api.tenant_of(self.tenant), {"decide"})

    def step_up(self) -> dict[str, Any]:
        body: dict[str, Any] = self.ask(amount=STEP_UP).json()
        assert body["outcome"] == "step_up"
        return body

    def list(self, headers: dict[str, str] | None = None, **params: Any):  # type: ignore[no-untyped-def]
        return self.api.client.get(
            "/v1/consent-requests", headers=headers or self.consent, params=params
        )


@pytest.fixture
def consents(api: Api, tmp_path: Path) -> Consents:
    api.use_packs(phase_1_packs(tmp_path))
    return Consents(api)


def test_the_pending_list_needs_a_consent_key(consents: Consents) -> None:
    consents.step_up()

    assert_problem(consents.list(consents.decide, status="pending"), 403, "INSUFFICIENT_SCOPE")
    assert consents.list(status="pending").status_code == 200


def test_a_pending_consent_request_is_listed_with_its_decision_and_expiry(
    consents: Consents,
) -> None:
    decision = consents.step_up()

    page = consents.list(status="pending").json()

    assert page == {
        "data": [
            {
                "consent_request_id": decision["step_up"]["consent_request_id"],
                "decision_id": decision["decision_id"],
                "status": "pending",
                "created_at": "2026-11-02T09:30:00Z",
                "expires_at": "2026-11-02T09:40:00Z",
            }
        ],
        "next_cursor": None,
    }


def test_expired_requests_are_not_pending(consents: Consents) -> None:
    old = consents.step_up()
    consents.api.clock.advance(timedelta(minutes=10))
    new = consents.step_up()

    pending = consents.list(status="pending").json()["data"]
    expired = consents.list(status="expired").json()["data"]
    everything = consents.list().json()["data"]

    assert [c["decision_id"] for c in pending] == [new["decision_id"]]
    assert [c["decision_id"] for c in expired] == [old["decision_id"]]
    assert [c["decision_id"] for c in everything] == [new["decision_id"], old["decision_id"]]


def test_the_list_pages_newest_first(consents: Consents) -> None:
    made = []
    for _ in range(3):
        made.append(consents.step_up()["decision_id"])
        consents.api.clock.advance(timedelta(seconds=1))

    first = consents.list(status="pending", limit=2).json()
    second = consents.list(status="pending", limit=2, cursor=first["next_cursor"]).json()

    assert [c["decision_id"] for c in first["data"]] == made[:0:-1]
    assert [c["decision_id"] for c in second["data"]] == made[:1]
    assert second["next_cursor"] is None


def test_a_bad_cursor_or_status_is_rejected(consents: Consents) -> None:
    assert_problem(consents.list(cursor="not-a-cursor"), 422, "VALIDATION_FAILED")
    assert consents.list(status="cancelled").status_code == 422


def test_another_tenants_requests_are_never_listed_or_read(api: Api, tmp_path: Path) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    mine, theirs = Consents(api, "mine"), Consents(api, "theirs")
    csr = theirs.step_up()["step_up"]["consent_request_id"]

    assert mine.list().json()["data"] == []
    response = api.client.get(f"/v1/consent-requests/{csr}", headers=mine.consent)
    assert_problem(response, 404, "CONSENT_REQUEST_NOT_FOUND")


def test_any_key_can_read_one_consent_request(consents: Consents) -> None:
    decision = consents.step_up()
    csr = decision["step_up"]["consent_request_id"]

    for headers in (consents.decide, consents.consent):
        body = consents.api.client.get(f"/v1/consent-requests/{csr}", headers=headers).json()
        assert (body["consent_request_id"], body["status"]) == (csr, "pending")
