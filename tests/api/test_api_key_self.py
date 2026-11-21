"""Key introspection, GET /v1/api-keys/self (#244): any key can read its own non-secret
details. The MCP server's startup guard uses it to refuse anything but a `decide` key."""

import pytest

from core.domain.parties import ApiKeyScope, hash_api_key
from tests.api.conftest import Api
from tests.api.test_tenants_payers_agents import assert_problem

SELF = "/v1/api-keys/self"


def _key(headers: dict[str, str]) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


@pytest.mark.parametrize(
    "scopes",
    [
        {"decide"},
        {"consent"},
        {"manage"},
        {"decide", "consent"},
        {"consent", "manage"},
    ],
    ids=["decide", "consent", "manage", "decide+consent", "consent+manage"],
)
def test_a_key_of_any_scope_reads_its_own_details(api: Api, scopes: set[ApiKeyScope]) -> None:
    tenant_id = api.tenant_of(api.new_tenant())
    headers = api.new_key(tenant_id, scopes)

    response = api.client.get(SELF, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "tenant_id": tenant_id,
        "key_prefix": _key(headers)[:16],
        "scopes": sorted(scopes),
        "created_at": body["created_at"],
    }
    assert body["created_at"].endswith("Z") or "+00:00" in body["created_at"]


def test_a_key_from_before_scopes_reports_all_three(api: Api) -> None:
    # Keys created before scopes existed were given every scope (migration 0008).
    headers = api.new_tenant()

    body = api.client.get(SELF, headers=headers).json()

    assert body["scopes"] == ["consent", "decide", "manage"]


def test_the_response_never_holds_the_secret_or_its_hash(api: Api) -> None:
    headers = api.new_tenant()
    key = _key(headers)

    text = api.client.get(SELF, headers=headers).text

    assert key not in text
    assert hash_api_key(key) not in text
    assert key[16:] not in text


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer apg_test_not-a-real-key"}, {"Authorization": "Basic x"}],
    ids=["missing", "unknown", "wrong-scheme"],
)
def test_a_missing_or_unknown_key_gets_401(api: Api, headers: dict[str, str]) -> None:
    assert_problem(api.client.get(SELF, headers=headers), 401, "UNAUTHENTICATED")


def test_the_details_are_read_within_the_keys_tenant(api: Api) -> None:
    mine = api.new_tenant("mine")
    other = api.tenant_of(api.new_tenant("other"))
    key_hash = hash_api_key(_key(mine))

    assert api.store.api_key_details(api.tenant_of(mine), key_hash) is not None
    assert api.store.api_key_details(other, key_hash) is None
