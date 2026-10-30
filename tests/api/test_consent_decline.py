"""POST /v1/consent-requests/{id}/decline: the Tenant records that its Payer said no.

The decline is a Consent resolution: a new Audit record in the Tenant's chain, never a
change to the Decision (ADR-0009). The `api` fixture replays and verifies every chain
when each test ends, so declined records must pass both."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.api.conftest import Api
from tests.api.test_consent_requests import STEP_UP, Consents
from tests.api.test_tenants_payers_agents import assert_problem
from tests.packs import phase_1_packs

ATTESTATION = {
    "method": "in_app_confirm",
    "reference": "app-session-42",
    "captured_at": "2026-11-02T09:31:00Z",
}


class Declines(Consents):
    def decline(self, csr: str, body: Any = None, *, key: str | None = None, headers=None):  # type: ignore[no-untyped-def]
        path = f"/v1/consent-requests/{csr}/decline"
        headers = headers or self.consent
        if key is None:
            return self.api.post(path, headers, body)
        return self.api.client.post(path, headers={**headers, "Idempotency-Key": key}, json=body)

    def decision(self, decision_id: str) -> dict[str, Any]:
        body: dict[str, Any] = self.api.client.get(
            f"/v1/decisions/{decision_id}", headers=self.decide
        ).json()
        return body


@pytest.fixture
def declines(api: Api, tmp_path: Path) -> Declines:
    api.use_packs(phase_1_packs(tmp_path))
    return Declines(api)


def test_a_decline_is_recorded_and_the_decision_reads_declined(declines: Declines) -> None:
    decision = declines.step_up()
    csr = decision["step_up"]["consent_request_id"]
    declines.api.clock.advance(timedelta(minutes=2))

    response = declines.decline(csr, {"attestation": ATTESTATION})

    assert response.status_code == 200
    resolution = response.json()
    assert resolution["consent_request_id"] == csr
    assert resolution["decision_id"] == decision["decision_id"]
    assert resolution["status"] == "declined"
    assert resolution["resolved_at"] == "2026-11-02T09:32:00Z"
    assert resolution["resolved_via"] == "tenant_api"
    assert resolution["audit_record_id"].startswith("aud_")
    after = declines.decision(decision["decision_id"])
    assert after["outcome"] == "step_up"  # the Decision itself never changes
    assert after["consent"] == {"status": "declined", "resolved_at": "2026-11-02T09:32:00Z"}


def test_a_decline_needs_no_attestation_or_body(declines: Declines) -> None:
    csr = declines.step_up()["step_up"]["consent_request_id"]

    assert declines.decline(csr).json()["status"] == "declined"


def test_the_decline_joins_the_audit_chain_with_its_attestation(declines: Declines) -> None:
    decision = declines.step_up()
    csr = decision["step_up"]["consent_request_id"]

    resolution = declines.decline(csr, {"attestation": ATTESTATION}).json()

    record = declines.audit(resolution["audit_record_id"]).record
    assert record.kind == "consent_resolution"
    assert record.seq == 2  # right after the step_up Decision
    assert record.decision_id == decision["decision_id"]
    assert record.recorded_at.isoformat() == "2026-11-02T09:30:00+00:00"
    assert record.resolution.consent_request_id == csr
    assert record.resolution.status == "declined"
    assert record.resolution.resolved_via == "tenant_api"
    assert record.resolution.attestation.reference == "app-session-42"
    assert (record.inputs, record.pack, record.output) == (None, None, None)


def test_a_decline_at_the_moment_of_expiry_is_too_late(declines: Declines) -> None:
    csr = declines.step_up()["step_up"]["consent_request_id"]

    declines.api.clock.advance(timedelta(minutes=9, seconds=59, microseconds=999_000))
    assert declines.decline(csr).status_code == 200

    later = declines.step_up()["step_up"]["consent_request_id"]
    declines.api.clock.advance(timedelta(minutes=10))
    assert_problem(declines.decline(later), 409, "CONSENT_EXPIRED")


def test_the_first_resolution_stands(declines: Declines) -> None:
    csr = declines.step_up()["step_up"]["consent_request_id"]
    first = declines.decline(csr, key="decline-1")

    replayed = declines.decline(csr, key="decline-1")
    again = declines.decline(csr, key="decline-2")

    assert replayed.json() == first.json()
    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert_problem(again, 409, "CONSENT_ALREADY_RESOLVED")


def test_an_agent_key_cannot_decline(declines: Declines) -> None:
    csr = declines.step_up()["step_up"]["consent_request_id"]

    assert_problem(declines.decline(csr, headers=declines.decide), 403, "INSUFFICIENT_SCOPE")


def test_another_tenants_consent_request_is_404(api: Api, tmp_path: Path) -> None:
    api.use_packs(phase_1_packs(tmp_path))
    mine, theirs = Declines(api, "mine"), Declines(api, "theirs")
    csr = theirs.step_up()["step_up"]["consent_request_id"]

    assert_problem(mine.decline(csr), 404, "CONSENT_REQUEST_NOT_FOUND")


def test_an_attestation_with_an_unknown_method_is_rejected(declines: Declines) -> None:
    csr = declines.step_up()["step_up"]["consent_request_id"]

    response = declines.decline(csr, {"attestation": {**ATTESTATION, "method": "email"}})

    assert_problem(response, 422, "VALIDATION_FAILED")


def test_a_declined_request_is_listed_as_declined_not_pending(declines: Declines) -> None:
    kept = declines.step_up()
    declined = declines.step_up()
    declines.decline(declined["step_up"]["consent_request_id"])

    pending = declines.list(status="pending").json()["data"]
    listed = declines.list(status="declined").json()["data"]

    assert [c["decision_id"] for c in pending] == [kept["decision_id"]]
    assert [(c["decision_id"], c["status"]) for c in listed] == [
        (declined["decision_id"], "declined")
    ]


def test_a_declined_step_up_does_not_count_in_spent_this_month(declines: Declines) -> None:
    # Monthly cap INR 50,000; cap-near at 80% (INR 40,000).
    declines.decline(declines.step_up()["step_up"]["consent_request_id"])
    for _ in range(9):
        assert declines.ask(amount={"minor": 4_000_00, "currency": "INR"}).json()["outcome"] == (
            "allow"
        )

    spent = declines.audit(
        declines.ask(amount={"minor": 1_00, "currency": "INR"}).json()["audit_record_id"]
    ).record.inputs.spent_this_month

    assert spent.minor == 36_000_00
    assert STEP_UP["minor"] == 6_000_00
