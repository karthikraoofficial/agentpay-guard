from datetime import timedelta
from typing import Any

import pytest

from tests.api.conftest import NOW, Api
from tests.api.test_tenants_payers_agents import assert_problem


def _iso(delta: timedelta = timedelta()) -> str:
    return (NOW + delta).isoformat().replace("+00:00", "Z")


class Setup:
    """A Tenant with one Payer and two Agents, ready to grant Mandates."""

    def __init__(self, api: Api, name: str = "acme") -> None:
        self.api = api
        self.tenant = api.new_tenant(name)
        self.payer_id = api.post(
            "/v1/payers", self.tenant, {"display_name": "Asha", "contact_ref": "tok_1"}
        ).json()["payer_id"]
        self.agent_ids = [
            api.post("/v1/agents", self.tenant, {"name": n}).json()["agent_id"]
            for n in ("ShopBot", "TravelBot")
        ]

    def body(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "payer_id": self.payer_id,
            "allowed_agent_ids": self.agent_ids,
            "currency": "INR",
            "per_txn_cap": {"minor": 10_000_00, "currency": "INR"},
            "monthly_cap": {"minor": 50_000_00, "currency": "INR"},
            "allowed_mccs": ["5411", "5812"],
            "valid_from": _iso(),
            "expires_at": _iso(timedelta(days=90)),
            "consent_evidence": {
                "method": "upi_mandate_ref",
                "reference": "UPI-MND-0001",
                "captured_at": _iso(-timedelta(minutes=5)),
            },
        }
        body.update(overrides)
        return body

    def create(self, **overrides: Any) -> dict[str, Any]:
        response = self.api.post("/v1/mandates", self.tenant, self.body(**overrides))
        assert response.status_code == 201, response.text
        mandate: dict[str, Any] = response.json()
        return mandate

    def get(self, mandate_id: str):  # type: ignore[no-untyped-def]
        return self.api.client.get(f"/v1/mandates/{mandate_id}", headers=self.tenant)


@pytest.fixture
def setup(api: Api) -> Setup:
    return Setup(api)


# --- Create and read -----------------------------------------------------------


def test_a_valid_mandate_is_created_with_its_consent_evidence(setup: Setup) -> None:
    mandate = setup.create()

    assert mandate["mandate_id"].startswith("mnd_")
    assert mandate["status"] == "active"
    assert mandate["payer_id"] == setup.payer_id
    assert mandate["allowed_agent_ids"] == setup.agent_ids
    assert mandate["per_txn_cap"] == {"minor": 10_000_00, "currency": "INR"}
    assert mandate["consent_evidence"] == {
        "method": "upi_mandate_ref",
        "reference": "UPI-MND-0001",
        "captured_at": "2026-11-02T09:25:00Z",
    }
    assert (mandate["revoked_by"], mandate["revoked_at"], mandate["supersedes"]) == (
        None,
        None,
        None,
    )
    assert setup.get(mandate["mandate_id"]).json() == mandate


INVALID: dict[str, dict[str, Any]] = {
    "float-amount": {"per_txn_cap": {"minor": 1000.5, "currency": "INR"}},
    "mcc-not-4-digits": {"allowed_mccs": ["54111"]},
    "mcc-unquoted": {"allowed_mccs": [5411]},
    "no-mccs": {"allowed_mccs": []},
    "no-agents": {"allowed_agent_ids": []},
    "unsupported-currency": {
        "currency": "EUR",
        "per_txn_cap": {"minor": 100_00, "currency": "EUR"},
        "monthly_cap": {"minor": 500_00, "currency": "EUR"},
    },
    "cap-in-another-currency": {"monthly_cap": {"minor": 500_00, "currency": "USD"}},
    "monthly-below-per-txn": {"monthly_cap": {"minor": 9_999_99, "currency": "INR"}},
    "expiry-over-a-year": {"expires_at": _iso(timedelta(days=366))},
    "expiry-before-start": {"expires_at": _iso(-timedelta(seconds=1))},
    "naive-datetime": {"valid_from": "2026-11-02T09:30:00"},
    "missing-consent-evidence": {"consent_evidence": None},
    "unknown-consent-method": {
        "consent_evidence": {"method": "email", "reference": "x", "captured_at": _iso()}
    },
}


@pytest.mark.parametrize("overrides", INVALID.values(), ids=INVALID.keys())
def test_an_invalid_mandate_is_refused(setup: Setup, overrides: dict[str, Any]) -> None:
    body = {k: v for k, v in setup.body(**overrides).items() if v is not None}

    assert_problem(setup.api.post("/v1/mandates", setup.tenant, body), 422, "VALIDATION_FAILED")


def test_a_mandate_naming_an_unknown_payer_or_agent_is_refused(setup: Setup) -> None:
    other = Setup(setup.api, "other")

    for overrides in (
        {"payer_id": "pay_unknown"},
        {"payer_id": other.payer_id},
        {"allowed_agent_ids": [setup.agent_ids[0], "agt_unknown"]},
        {"allowed_agent_ids": other.agent_ids},
    ):
        response = setup.api.post("/v1/mandates", setup.tenant, setup.body(**overrides))
        assert_problem(response, 422, "VALIDATION_FAILED")


def test_a_mandate_from_another_tenant_is_not_found(setup: Setup) -> None:
    mandate_id = setup.create()["mandate_id"]
    other = setup.api.new_tenant("other")

    response = setup.api.client.get(f"/v1/mandates/{mandate_id}", headers=other)

    assert_problem(response, 404, "MANDATE_NOT_FOUND")


# --- Status is computed from time ----------------------------------------------


def test_status_follows_the_validity_window_without_any_write(setup: Setup) -> None:
    mandate_id = setup.create(valid_from=_iso(timedelta(days=1)))["mandate_id"]

    assert setup.get(mandate_id).json()["status"] == "not_yet_valid"
    setup.api.clock.advance(timedelta(days=1))
    assert setup.get(mandate_id).json()["status"] == "active"
    setup.api.clock.advance(timedelta(days=90))
    assert setup.get(mandate_id).json()["status"] == "expired"


# --- Revocation -----------------------------------------------------------------


def test_revocation_records_who_and_when_and_is_final(setup: Setup) -> None:
    mandate_id = setup.create()["mandate_id"]
    revoke = f"/v1/mandates/{mandate_id}/revoke"
    headers = {**setup.tenant, "Idempotency-Key": "k-revoke"}

    first = setup.api.client.post(revoke, headers=headers, json={"revoked_by": "payer"})
    replay = setup.api.client.post(revoke, headers=headers, json={"revoked_by": "payer"})
    again = setup.api.post(revoke, setup.tenant, {"revoked_by": "tenant"})

    assert first.status_code == 200
    revoked = first.json()
    assert (revoked["status"], revoked["revoked_by"]) == ("revoked", "payer")
    assert revoked["revoked_at"] == "2026-11-02T09:30:00Z"
    assert replay.content == first.content
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert_problem(again, 409, "MANDATE_ALREADY_REVOKED")
    assert setup.get(mandate_id).json()["revoked_by"] == "payer"


def test_revoking_an_unknown_mandate_is_not_found(setup: Setup) -> None:
    response = setup.api.post(
        "/v1/mandates/mnd_unknown/revoke", setup.tenant, {"revoked_by": "payer"}
    )

    assert_problem(response, 404, "MANDATE_NOT_FOUND")


@pytest.mark.parametrize("method", ["PATCH", "PUT"])
def test_no_route_changes_a_mandates_terms(setup: Setup, method: str) -> None:
    mandate_id = setup.create()["mandate_id"]

    response = setup.api.client.request(
        method,
        f"/v1/mandates/{mandate_id}",
        headers={**setup.tenant, "Idempotency-Key": "k"},
        json={"monthly_cap": {"minor": 1, "currency": "INR"}},
    )

    assert_problem(response, 405, "METHOD_NOT_ALLOWED")


# --- Superseding ----------------------------------------------------------------


def test_a_superseding_mandate_points_to_the_one_it_replaces(setup: Setup) -> None:
    old = setup.create()["mandate_id"]
    setup.api.post(f"/v1/mandates/{old}/revoke", setup.tenant, {"revoked_by": "payer"})

    new = setup.create(supersedes=old, monthly_cap={"minor": 80_000_00, "currency": "INR"})

    assert new["supersedes"] == old
    assert setup.get(old).json()["status"] == "revoked"


def test_supersedes_must_name_a_mandate_of_the_same_payer(setup: Setup) -> None:
    other_payer = setup.api.post(
        "/v1/payers", setup.tenant, {"display_name": "Ravi", "contact_ref": "tok_2"}
    ).json()["payer_id"]
    ravis = setup.create(payer_id=other_payer)["mandate_id"]

    for supersedes in ("mnd_unknown", ravis):
        response = setup.api.post("/v1/mandates", setup.tenant, setup.body(supersedes=supersedes))
        assert_problem(response, 422, "VALIDATION_FAILED")


# --- Listing ---------------------------------------------------------------------


def test_a_payers_mandates_are_listed_newest_first_by_cursor(setup: Setup) -> None:
    created = []
    for _ in range(5):
        created.append(setup.create()["mandate_id"])
        setup.api.clock.advance(timedelta(seconds=1))
    other = Setup(setup.api, "other")
    other.create()

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"payer_id": setup.payer_id, "limit": "2"} | ({"cursor": cursor} if cursor else {})
        page = setup.api.client.get("/v1/mandates", headers=setup.tenant, params=params).json()
        seen += [m["mandate_id"] for m in page["data"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == list(reversed(created))
    assert pages == 3


def test_listing_another_tenants_payer_returns_nothing(setup: Setup) -> None:
    setup.create()
    other = setup.api.new_tenant("other")

    page = setup.api.client.get(
        "/v1/mandates", headers=other, params={"payer_id": setup.payer_id}
    ).json()

    assert page == {"data": [], "next_cursor": None}
