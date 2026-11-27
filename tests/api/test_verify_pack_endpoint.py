"""GET /v1/disputes/{id}/verify-pack (#283): the inspector's check of a Dispute's Evidence
packs, the same check as `api.admin verify-pack` (core.pack_verify), then the Tenant's
Audit chain. A tampered pack is caught in tests/api/test_verify_pack_cli.py, which edits
the table as a superuser would and reads both the command and this endpoint."""

from typing import Any

from tests.api.test_disputes import Disputes, disputes  # noqa: F401  (the fixture)


def _check(disputes: Disputes, dispute_id: str, headers: dict[str, str] | None = None) -> Any:  # noqa: F811
    return disputes.api.client.get(
        f"/v1/disputes/{dispute_id}/verify-pack", headers=headers or disputes.manage
    )


def test_a_signed_off_pack_verifies_against_its_sign_off_and_the_chain(
    disputes: Disputes,  # noqa: F811
) -> None:
    dispute = disputes.built(disputes.captured())
    disputes.act(dispute["dispute_id"], "regenerate")
    disputes.build(dispute["dispute_id"])
    disputes.sign_off(dispute["dispute_id"], pack_version=2)

    checked = _check(disputes, dispute["dispute_id"])

    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert (body["status"], body["clean"]) == ("clean", True)
    assert body["versions"] == [
        {"version": 1, "signed_off": False, "problems": []},
        {"version": 2, "signed_off": True, "problems": []},
    ]
    assert body["pack_problems"] == []
    assert body["chain"]["clean"] is True


def test_a_dispute_with_no_pack_yet_has_no_versions(disputes: Disputes) -> None:  # noqa: F811
    opened = disputes.open(disputes.captured()["payment_id"]).json()

    body = _check(disputes, opened["dispute_id"]).json()

    assert body["versions"] == []
    assert body["status"] == "clean"


def test_only_the_disputes_own_tenant_can_check_it(disputes: Disputes) -> None:  # noqa: F811
    dispute = disputes.built(disputes.captured())
    other = Disputes(disputes.api, "globex")

    assert _check(other, dispute["dispute_id"]).status_code == 404
    assert _check(other, dispute["dispute_id"]).json()["code"] == "DISPUTE_NOT_FOUND"


def test_an_agent_key_cant_check_a_pack(disputes: Disputes) -> None:  # noqa: F811
    dispute = disputes.built(disputes.captured())

    decide_only = disputes.api.new_key(disputes.tenant_id(), {"decide"})

    refused = _check(disputes, dispute["dispute_id"], decide_only)

    assert refused.status_code == 403
