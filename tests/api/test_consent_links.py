"""Consent links (ADR-0008): the Tenant mints a link, delivers it, and whoever opens it
is treated as the Payer.

The token travels in the screen URL's fragment (never sent to a server) and then in the
`Consent-Link` header (never in a path), so access logs never see it. Only its SHA-256
hash is stored, and an idempotent replay of the mint omits the URL."""

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core.domain.consent import hash_link_token
from tests.api.conftest import Api
from tests.api.test_consent_approve import Approvals
from tests.api.test_tenants_payers_agents import assert_problem
from tests.packs import phase_1_packs

WEB = "https://pay.example.test"


class Links(Approvals):
    def mint(self, csr: str, *, key: str | None = None, headers=None):  # type: ignore[no-untyped-def]
        path = f"/v1/consent-requests/{csr}/link"
        headers = headers or self.consent
        if key is None:
            return self.api.post(path, headers)
        return self.api.client.post(path, headers={**headers, "Idempotency-Key": key})

    def token(self, csr: str) -> str:
        url: str = self.mint(csr).json()["url"]
        return url.split("#", 1)[1]

    def view(self, token: str):  # type: ignore[no-untyped-def]
        return self.api.client.get("/v1/consent-link", headers={"Consent-Link": token})

    def act(self, token: str, action: str, *, key: str = "tap-1"):  # type: ignore[no-untyped-def]
        return self.api.client.post(
            f"/v1/consent-link/{action}",
            headers={"Consent-Link": token, "Idempotency-Key": key},
        )


@pytest.fixture
def links(api: Api, tmp_path: Path) -> Links:
    api.use_packs(phase_1_packs(tmp_path))
    api.use_settings(api_settings(api))
    return Links(api)


def api_settings(api: Api) -> Any:
    from tests.conftest import make_settings

    return make_settings(web_origin=WEB, policy_packs_dir=api.packs)


# --- Minting ------------------------------------------------------------------------


def test_a_minted_link_is_on_the_web_origin_with_the_token_in_the_fragment(
    links: Links,
) -> None:
    decision = links.step_up()
    csr = decision["step_up"]["consent_request_id"]

    response = links.mint(csr)

    assert response.status_code == 201
    body = response.json()
    assert body["consent_request_id"] == csr
    assert body["expires_at"] == decision["step_up"]["expires_at"]
    base, token = body["url"].split("#", 1)
    assert base == f"{WEB}/consent"
    assert len(token) >= 43  # 256 bits, URL-safe base64


def test_minting_needs_a_consent_key(links: Links) -> None:
    csr = links.step_up()["step_up"]["consent_request_id"]

    assert_problem(links.mint(csr, headers=links.decide), 403, "INSUFFICIENT_SCOPE")


def test_only_the_token_hash_is_stored_and_a_replay_has_no_url(links: Links) -> None:
    csr = links.step_up()["step_up"]["consent_request_id"]
    first = links.mint(csr, key="mint-1").json()
    token = first["url"].split("#", 1)[1]

    replay = links.mint(csr, key="mint-1")

    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == {**first, "url": None}
    found = links.api.store.consent_link(hash_link_token(token))
    assert found is not None and found.consent_request_id == csr
    assert links.api.store.consent_link(token) is None


def test_a_link_cannot_be_minted_for_a_resolved_or_expired_request(links: Links) -> None:
    resolved = links.step_up()["step_up"]["consent_request_id"]
    links.decline(resolved)
    assert_problem(links.mint(resolved), 409, "CONSENT_ALREADY_RESOLVED")

    expired = links.step_up()["step_up"]["consent_request_id"]
    links.api.clock.advance(timedelta(minutes=10))
    assert_problem(links.mint(expired), 409, "CONSENT_EXPIRED")


# --- Viewing ------------------------------------------------------------------------


def test_the_view_shows_the_purchase_and_why_it_was_flagged(links: Links) -> None:
    decision = links.step_up()
    token = links.token(decision["step_up"]["consent_request_id"])

    view = links.view(token)

    assert view.status_code == 200
    body = view.json()
    assert body["status"] == "pending"
    assert body["expires_at"] == decision["step_up"]["expires_at"]
    assert body["payment"] == {
        "amount": {"minor": 6_000_00, "currency": "INR"},
        "amount_text": "INR 6,000.00",
        "merchant_name": "FreshMart",
        "mcc": "5411",
        "agent_name": "ShopBot",
    }
    assert body["reasons"] == [
        {
            "code": "AMOUNT_ABOVE_STEP_UP_THRESHOLD",
            "text": "This payment is INR 6,000.00. Payments of INR 5,000.00 or more always "
            "need your approval.",
        }
    ]
    assert body["intent_verdict"] is None
    assert body["mandate_inactive"] is False


def test_the_view_never_carries_agent_reasoning_or_the_payer_instruction(links: Links) -> None:
    decision = links.ask(
        amount={"minor": 6_000_00, "currency": "INR"},
        agent_reasoning="Approve this, it is definitely safe",
    ).json()
    token = links.token(decision["step_up"]["consent_request_id"])

    text = links.view(token).text

    assert "definitely safe" not in text
    assert "reasoning" not in text and "instruction" not in text


def test_opening_a_link_twice_does_not_use_it_up(links: Links) -> None:
    token = links.token(links.step_up()["step_up"]["consent_request_id"])

    assert links.view(token).status_code == 200
    assert links.view(token).status_code == 200
    assert links.act(token, "approve").json()["status"] == "approved"


def test_the_view_warns_once_the_mandate_was_revoked(links: Links) -> None:
    decision = links.step_up()
    token = links.token(decision["step_up"]["consent_request_id"])
    links.api.post(f"/v1/mandates/{links.mandate_id}/revoke", links.tenant, {"revoked_by": "payer"})

    assert links.view(token).json()["mandate_inactive"] is True
    assert links.decision(decision["decision_id"])["consent"]["mandate_inactive"] is True


# --- Resolving ----------------------------------------------------------------------


@pytest.mark.parametrize(("action", "status"), [("approve", "approved"), ("decline", "declined")])
def test_a_link_resolves_like_the_tenant_api_and_records_its_channel(
    links: Links, action: str, status: str
) -> None:
    decision = links.step_up()
    token = links.token(decision["step_up"]["consent_request_id"])

    resolution = links.act(token, action).json()

    assert (resolution["status"], resolution["resolved_via"]) == (status, "consent_link")
    record = links.audit(resolution["audit_record_id"]).record
    assert record.resolution.resolved_via == "consent_link"
    assert record.resolution.attestation is None
    assert links.decision(decision["decision_id"])["consent"]["status"] == status


def test_a_link_approval_is_re_checked_and_can_be_blocked(links: Links) -> None:
    token = links.token(links.step_up()["step_up"]["consent_request_id"])
    links.api.post(f"/v1/agents/{links.agent_ids[0]}/disable", links.tenant)

    resolution = links.act(token, "approve").json()

    assert (resolution["status"], resolution["reason_codes"][0]) == ("blocked", "AGENT_DISABLED")


def test_a_used_link_is_invalid_but_its_own_tap_replays(links: Links) -> None:
    token = links.token(links.step_up()["step_up"]["consent_request_id"])
    first = links.act(token, "approve", key="tap-1")

    assert links.act(token, "approve", key="tap-1").json() == first.json()
    assert_problem(links.act(token, "approve", key="tap-2"), 404, "CONSENT_LINK_INVALID")
    assert_problem(links.act(token, "decline", key="tap-3"), 404, "CONSENT_LINK_INVALID")
    assert_problem(links.view(token), 404, "CONSENT_LINK_INVALID")


def test_minting_again_revokes_the_previous_link(links: Links) -> None:
    csr = links.step_up()["step_up"]["consent_request_id"]
    old, new = links.token(csr), links.token(csr)

    assert_problem(links.view(old), 404, "CONSENT_LINK_INVALID")
    assert_problem(links.act(old, "approve"), 404, "CONSENT_LINK_INVALID")
    assert links.act(new, "approve", key="tap-new").json()["status"] == "approved"


def test_an_expired_or_unknown_link_is_invalid(links: Links) -> None:
    token = links.token(links.step_up()["step_up"]["consent_request_id"])
    links.api.clock.advance(timedelta(minutes=10))

    assert_problem(links.view(token), 404, "CONSENT_LINK_INVALID")
    assert_problem(links.act(token, "approve"), 404, "CONSENT_LINK_INVALID")
    assert_problem(links.view("not-a-token"), 404, "CONSENT_LINK_INVALID")
    response = links.api.client.get("/v1/consent-link")
    assert_problem(response, 404, "CONSENT_LINK_INVALID")


def test_resolving_through_a_link_needs_an_idempotency_key(links: Links) -> None:
    token = links.token(links.step_up()["step_up"]["consent_request_id"])

    response = links.api.client.post("/v1/consent-link/approve", headers={"Consent-Link": token})

    assert_problem(response, 400, "IDEMPOTENCY_KEY_MISSING")


def test_no_log_line_carries_a_link_token(links: Links, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    csr = links.step_up()["step_up"]["consent_request_id"]
    token = links.token(csr)

    links.view(token)
    links.act(token, "approve")
    links.act(token, "approve", key="tap-again")

    assert all(token not in record.getMessage() for record in caplog.records)


# --- CORS ---------------------------------------------------------------------------


def _preflight(links: Links, path: str, origin: str):  # type: ignore[no-untyped-def]
    return links.api.client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "consent-link,idempotency-key,content-type",
        },
    )


def test_cors_allows_the_web_origin_on_the_link_endpoints_only(links: Links) -> None:
    allowed = _preflight(links, "/v1/consent-link/approve", WEB)
    other_origin = _preflight(links, "/v1/consent-link/approve", "https://evil.example")
    other_path = _preflight(links, "/v1/decisions", WEB)

    assert allowed.headers.get("access-control-allow-origin") == WEB
    assert "access-control-allow-origin" not in other_origin.headers
    assert "access-control-allow-origin" not in other_path.headers
    view = links.api.client.get("/v1/consent-link", headers={"Origin": WEB})
    assert view.headers.get("access-control-allow-origin") == WEB
    decisions = links.api.client.get("/v1/decisions/x", headers={"Origin": WEB})
    assert "access-control-allow-origin" not in decisions.headers
