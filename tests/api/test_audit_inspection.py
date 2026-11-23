"""The inspector's read endpoints (#275): read an Audit record, Replay its Decision and
verify the Tenant's Audit chain, over HTTP, with the `manage` scope."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from adapters.postgres.store import PostgresStore
from api.main import create_app
from core.domain.parties import ApiKeyScope
from tests.api.conftest import NOW, Api, MovableClock
from tests.api.test_demo_controls import SMALL, _grant
from tests.api.test_replay_verify_cli import tamper
from tests.conftest import make_settings


def _decide(api: Api, headers: dict[str, str]) -> dict[str, Any]:
    mandate_id, agent_id = _grant(api, headers)
    response = api.post(
        "/v1/decisions",
        headers,
        {
            "mandate_id": mandate_id,
            "agent_id": agent_id,
            "amount": SMALL,
            "merchant": {"id": "m_freshmart", "name": "FreshMart", "mcc": "5411"},
        },
    )
    assert response.status_code == 200, response.text
    decided: dict[str, Any] = response.json()
    return decided


def test_a_decision_replays_identically(api: Api) -> None:
    headers = api.new_tenant()
    audit_id = _decide(api, headers)["audit_record_id"]

    replayed = api.client.get(f"/v1/audit-records/{audit_id}/replay", headers=headers)

    assert replayed.status_code == 200, replayed.text
    body = replayed.json()
    assert (body["audit_record_id"], body["status"]) == (audit_id, "identical")
    assert body["identical"] is True
    assert body["difference"] is None


@pytest.mark.parametrize("scopes", [{"decide"}, {"consent"}])
def test_replay_needs_the_manage_scope(api: Api, scopes: set[ApiKeyScope]) -> None:
    headers = api.new_tenant()
    audit_id = _decide(api, headers)["audit_record_id"]
    narrow = api.new_key(api.tenant_of(headers), frozenset(scopes))

    refused = api.client.get(f"/v1/audit-records/{audit_id}/replay", headers=narrow)

    assert refused.status_code == 403
    assert refused.json()["code"] == "INSUFFICIENT_SCOPE"


def test_another_tenants_record_is_not_found_for_replay(api: Api) -> None:
    audit_id = _decide(api, api.new_tenant("one"))["audit_record_id"]
    other = api.new_tenant("two")

    missing = api.client.get(f"/v1/audit-records/{audit_id}/replay", headers=other)

    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"


def test_an_audit_record_shows_its_rule_results_and_pack(api: Api) -> None:
    headers = api.new_tenant()
    decided = _decide(api, headers)
    audit_id = decided["audit_record_id"]

    read = api.client.get(f"/v1/audit-records/{audit_id}", headers=headers)

    assert read.status_code == 200, read.text
    body = read.json()
    assert (body["audit_record_id"], body["kind"]) == (audit_id, "decision")
    assert body["decision_id"] == decided["decision_id"]
    assert body["outcome"] == "allow"
    assert body["reason_codes"] == []
    assert body["policy_pack"] == decided["policy_pack"]
    assert body["rule_results"], "an engine run has Rule results"
    assert {r["result"] for r in body["rule_results"]} <= {"pass", "not_evaluated"}
    assert all(r["rule_id"] for r in body["rule_results"])
    assert "inputs" not in body  # the inspector is public: no Mandate or request details


def test_reading_a_record_needs_manage_and_the_right_tenant(api: Api) -> None:
    headers = api.new_tenant("one")
    audit_id = _decide(api, headers)["audit_record_id"]
    narrow = api.new_key(api.tenant_of(headers), frozenset({"decide"}))
    other = api.new_tenant("two")

    assert api.client.get(f"/v1/audit-records/{audit_id}", headers=narrow).status_code == 403
    assert api.client.get(f"/v1/audit-records/{audit_id}", headers=other).status_code == 404


def test_a_clean_chain_verifies_with_its_record_count(api: Api) -> None:
    headers = api.new_tenant()
    _decide(api, headers)
    _decide(api, headers)

    verified = api.client.get("/v1/audit-chain/verify", headers=headers)

    assert verified.status_code == 200, verified.text
    assert verified.json() == {"clean": True, "record_count": 2, "first_broken_seq": None}


def test_verify_counts_only_the_callers_tenant_and_needs_manage(api: Api) -> None:
    one = api.new_tenant("one")
    _decide(api, one)
    two = api.new_tenant("two")
    narrow = api.new_key(api.tenant_of(two), frozenset({"consent"}))

    assert api.client.get("/v1/audit-chain/verify", headers=two).json()["record_count"] == 0
    assert api.client.get("/v1/audit-chain/verify", headers=narrow).status_code == 403


def test_verify_names_the_first_broken_record(migrated_database_url: str) -> None:
    engine = create_engine(migrated_database_url)
    store = PostgresStore(engine)
    clock = MovableClock(NOW)
    api = Api(TestClient(create_app(make_settings(), store=store, clock=clock)), store, clock)
    headers = api.new_tenant()
    _decide(api, headers)
    _decide(api, headers)
    tamper(
        engine,
        "UPDATE audit_records SET record_hash = :bad WHERE tenant_id = :tenant AND seq = 2",
        bad="sha256:" + "00" * 32,
        tenant=api.tenant_of(headers),
    )

    verified = api.client.get("/v1/audit-chain/verify", headers=headers)

    assert verified.json() == {"clean": False, "record_count": 2, "first_broken_seq": 2}
