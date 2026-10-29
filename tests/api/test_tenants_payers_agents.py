import pytest

from tests.api.conftest import Api

PROBLEM_FIELDS = {"type", "title", "status", "code", "detail", "request_id"}


def assert_problem(response, status: int, code: str) -> None:  # type: ignore[no-untyped-def]
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert PROBLEM_FIELDS <= body.keys()
    assert (body["status"], body["code"]) == (status, code)


# --- Authentication ------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer apg_test_not-a-real-key"}, {"Authorization": "Basic abc"}],
    ids=["missing", "unknown-key", "wrong-scheme"],
)
def test_a_request_without_a_valid_key_is_unauthenticated(
    api: Api, headers: dict[str, str]
) -> None:
    response = api.post("/v1/payers", headers, {"display_name": "Asha", "contact_ref": "c_1"})

    assert_problem(response, 401, "UNAUTHENTICATED")


# --- Payers --------------------------------------------------------------------


def test_a_tenant_can_register_and_fetch_a_payer(api: Api) -> None:
    tenant = api.new_tenant()

    created = api.post("/v1/payers", tenant, {"display_name": "Asha", "contact_ref": "tok_9f2"})

    assert created.status_code == 201
    payer = created.json()
    assert payer["payer_id"].startswith("pay_")
    assert (payer["display_name"], payer["contact_ref"]) == ("Asha", "tok_9f2")
    fetched = api.client.get(f"/v1/payers/{payer['payer_id']}", headers=tenant)
    assert fetched.status_code == 200
    assert fetched.json() == payer


def test_a_payer_from_another_tenant_is_not_found(api: Api) -> None:
    owner, other = api.new_tenant("owner"), api.new_tenant("other")
    payer_id = api.post("/v1/payers", owner, {"display_name": "A", "contact_ref": "c"}).json()[
        "payer_id"
    ]

    assert_problem(api.client.get(f"/v1/payers/{payer_id}", headers=other), 404, "PAYER_NOT_FOUND")


@pytest.mark.parametrize(
    "contact_ref",
    ["asha@example.com", "+919876543210", "9876543210"],
    ids=["email", "e164-phone", "bare-phone"],
)
def test_a_raw_email_or_phone_number_is_refused_as_a_contact_ref(
    api: Api, contact_ref: str
) -> None:
    response = api.post(
        "/v1/payers", api.new_tenant(), {"display_name": "Asha", "contact_ref": contact_ref}
    )

    assert_problem(response, 422, "VALIDATION_FAILED")


# --- Agents --------------------------------------------------------------------


def test_a_tenant_can_register_disable_and_re_enable_an_agent(api: Api) -> None:
    tenant = api.new_tenant()

    created = api.post("/v1/agents", tenant, {"name": "ShopBot"})

    assert created.status_code == 201
    agent = created.json()
    assert agent["agent_id"].startswith("agt_")
    assert (agent["name"], agent["status"]) == ("ShopBot", "active")
    agent_url = f"/v1/agents/{agent['agent_id']}"

    disabled = api.post(f"{agent_url}/disable", tenant)
    assert (disabled.status_code, disabled.json()["status"]) == (200, "disabled")
    assert api.client.get(agent_url, headers=tenant).json()["status"] == "disabled"

    enabled = api.post(f"{agent_url}/enable", tenant)
    assert (enabled.status_code, enabled.json()["status"]) == (200, "active")
    assert api.client.get(agent_url, headers=tenant).json()["status"] == "active"


def test_an_agent_from_another_tenant_cannot_be_seen_or_disabled(api: Api) -> None:
    owner, other = api.new_tenant("owner"), api.new_tenant("other")
    agent_id = api.post("/v1/agents", owner, {"name": "ShopBot"}).json()["agent_id"]

    assert_problem(api.client.get(f"/v1/agents/{agent_id}", headers=other), 404, "AGENT_NOT_FOUND")
    assert_problem(api.post(f"/v1/agents/{agent_id}/disable", other), 404, "AGENT_NOT_FOUND")
    assert api.client.get(f"/v1/agents/{agent_id}", headers=owner).json()["status"] == "active"


# --- Errors and idempotency headers -------------------------------------------


def test_a_write_without_an_idempotency_key_is_refused(api: Api) -> None:
    response = api.client.post("/v1/agents", headers=api.new_tenant(), json={"name": "ShopBot"})

    assert_problem(response, 400, "IDEMPOTENCY_KEY_MISSING")


def test_a_body_that_is_not_json_is_malformed(api: Api) -> None:
    tenant = {**api.new_tenant(), "Idempotency-Key": "k", "Content-Type": "application/json"}

    response = api.client.post("/v1/agents", headers=tenant, content=b"{not json")

    assert_problem(response, 400, "MALFORMED_REQUEST")


def test_an_unknown_field_fails_validation(api: Api) -> None:
    response = api.post("/v1/agents", api.new_tenant(), {"name": "ShopBot", "tenant_id": "t_x"})

    assert_problem(response, 422, "VALIDATION_FAILED")
