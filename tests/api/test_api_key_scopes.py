"""API key scopes (ADR-0011): an Agent's `decide` key can ask for Decisions and Intent
checks but can't grant power or resolve consent; `manage` and `consent` keys can't act
as an Agent."""

import pytest

from core.domain.parties import ApiKeyScope
from tests.api.conftest import Api
from tests.api.test_intents import Intents
from tests.api.test_tenants_payers_agents import assert_problem


class Scoped(Intents):
    """A Tenant set up with its all-scope key, plus one key per single scope."""

    def __init__(self, api: Api) -> None:
        super().__init__(api)
        tenant_id = api.tenant_of(self.tenant)
        self.keys: dict[ApiKeyScope, dict[str, str]] = {
            scope: api.new_key(tenant_id, {scope}) for scope in ("decide", "consent", "manage")
        }

    def as_(self, scope: ApiKeyScope) -> "Scoped":
        self.tenant = self.keys[scope]
        return self


@pytest.fixture
def scoped(api: Api) -> Scoped:
    return Scoped(api)


def _manage_writes(s: Scoped) -> list[tuple[str, object]]:
    agent = s.agent_ids[1]
    return [
        ("/v1/payers", {"display_name": "Ravi", "contact_ref": "tok_2"}),
        ("/v1/agents", {"name": "NewBot"}),
        (f"/v1/agents/{agent}/disable", None),
        (f"/v1/agents/{agent}/enable", None),
        ("/v1/mandates", s.body()),
    ]


@pytest.mark.parametrize("scope", ["decide", "consent"])
def test_only_a_manage_key_can_register_parties_grant_mandates_or_switch_agents(
    scoped: Scoped, scope: ApiKeyScope
) -> None:
    for path, body in _manage_writes(scoped):
        response = scoped.api.post(path, scoped.keys[scope], body)
        assert_problem(response, 403, "INSUFFICIENT_SCOPE")


def test_a_manage_key_can_register_parties_grant_mandates_and_switch_agents(
    scoped: Scoped,
) -> None:
    for path, body in _manage_writes(scoped):
        assert scoped.api.post(path, scoped.keys["manage"], body).status_code in (200, 201)


def test_a_decide_key_can_ask_for_decisions_and_intent_checks(scoped: Scoped) -> None:
    scoped.as_("decide")

    assert scoped.ask().json()["outcome"] == "allow"
    assert scoped.check().status_code == 201


@pytest.mark.parametrize("scope", ["consent", "manage"])
def test_only_a_decide_key_can_act_as_an_agent(scoped: Scoped, scope: ApiKeyScope) -> None:
    scoped.as_(scope)

    assert_problem(scoped.ask(), 403, "INSUFFICIENT_SCOPE")
    assert_problem(scoped.check(), 403, "INSUFFICIENT_SCOPE")


@pytest.mark.parametrize("scope", ["decide", "consent", "manage"])
def test_any_key_can_revoke_a_mandate(api: Api, scope: ApiKeyScope) -> None:
    s = Scoped(api)

    response = api.post(
        f"/v1/mandates/{s.mandate_id}/revoke", s.keys[scope], {"revoked_by": "payer"}
    )

    assert response.json()["status"] == "revoked"


@pytest.mark.parametrize("scope", ["decide", "consent", "manage"])
def test_any_key_can_read(scoped: Scoped, scope: ApiKeyScope) -> None:
    decision_id = scoped.ask().json()["decision_id"]
    headers = scoped.keys[scope]

    for path in (
        f"/v1/decisions/{decision_id}",
        f"/v1/mandates/{scoped.mandate_id}",
        f"/v1/mandates?payer_id={scoped.payer_id}",
        f"/v1/payers/{scoped.payer_id}",
        f"/v1/agents/{scoped.agent_ids[0]}",
    ):
        assert scoped.api.client.get(path, headers=headers).status_code == 200, path


def test_only_a_manage_key_can_read_an_intent_verdict(scoped: Scoped) -> None:
    intent_id = scoped.check().json()["intent_id"]
    path = f"/v1/intents/{intent_id}"

    for scope in ("decide", "consent"):
        response = scoped.api.client.get(path, headers=scoped.keys[scope])
        assert_problem(response, 403, "INSUFFICIENT_SCOPE")
    assert scoped.api.client.get(path, headers=scoped.keys["manage"]).status_code == 200


def test_an_unknown_key_is_still_unauthenticated_not_forbidden(api: Api) -> None:
    response = api.client.post(
        "/v1/payers",
        headers={"Authorization": "Bearer apg_test_nope", "Idempotency-Key": "k"},
        json={"display_name": "A", "contact_ref": "t"},
    )

    assert_problem(response, 401, "UNAUTHENTICATED")
