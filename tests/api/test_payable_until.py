"""`payable_until`: the end of the Payment window, shown to the Agent (#78).

An allow is payable until its Decision time plus the pack's `payment_window_seconds`; an
approved step-up until its resolution time plus the window of the pack the approval was
re-checked with. Every other Outcome or resolution reads null. It is worked out at the
API edge, so the engine output and the Audit record bytes never carry it."""

from datetime import timedelta
from pathlib import Path

import pytest

from tests.api.conftest import Api
from tests.api.test_consent_approve import inr
from tests.api.test_consent_links import Links, api_settings
from tests.packs import release_test_pack


def window_pack(root: Path, version: str, seconds: int) -> Path:
    """default@1.0.0 (caps only, no Signals) with a Payment window of `seconds`."""
    return release_test_pack(root, version, {"payment_window_seconds": seconds})


@pytest.fixture
def links(api: Api, tmp_path: Path) -> Links:
    api.use_packs(window_pack(tmp_path, "1.0.1", 120))
    api.use_settings(api_settings(api))
    return Links(api)


def test_an_allow_is_payable_for_the_packs_window_from_the_decision_time(links: Links) -> None:
    body = links.ask().json()

    assert (body["outcome"], body["decided_at"]) == ("allow", "2026-11-02T09:30:00Z")
    assert body["payable_until"] == "2026-11-02T09:32:00Z"
    assert links.decision(body["decision_id"])["payable_until"] == "2026-11-02T09:32:00Z"


def test_the_active_default_pack_gives_fifteen_minutes(api: Api) -> None:
    from tests.api.test_decisions import Pay

    body = Pay(api).ask().json()

    assert body["policy_pack"]["version"] == "1.3.0"
    assert body["payable_until"] == "2026-11-02T09:45:00Z"


def test_a_step_up_or_a_block_is_not_payable(links: Links) -> None:
    step_up = links.step_up()
    links.api.post(f"/v1/mandates/{links.mandate_id}/revoke", links.tenant, {"revoked_by": "payer"})
    block = links.ask().json()

    assert step_up["payable_until"] is None
    assert links.decision(step_up["decision_id"])["payable_until"] is None
    assert block["outcome"] == "block"
    assert block["payable_until"] is None


def test_an_approval_is_payable_for_the_window_from_the_resolution_time(links: Links) -> None:
    decision = links.step_up()
    links.api.clock.advance(timedelta(minutes=3))

    resolution = links.approve(decision["step_up"]["consent_request_id"]).json()

    assert (resolution["status"], resolution["resolved_at"]) == ("approved", "2026-11-02T09:33:00Z")
    assert resolution["payable_until"] == "2026-11-02T09:35:00Z"
    assert links.decision(decision["decision_id"])["payable_until"] == "2026-11-02T09:35:00Z"


def test_an_approval_through_a_consent_link_is_payable_too(links: Links) -> None:
    token = links.token(links.step_up()["step_up"]["consent_request_id"])

    resolution = links.act(token, "approve").json()

    assert resolution["status"] == "approved"
    assert resolution["payable_until"] == "2026-11-02T09:32:00Z"


def test_a_declined_blocked_or_expired_step_up_is_not_payable(links: Links) -> None:
    declined = links.step_up()
    resolution = links.decline(declined["step_up"]["consent_request_id"]).json()
    assert (resolution["status"], resolution["payable_until"]) == ("declined", None)
    assert links.decision(declined["decision_id"])["payable_until"] is None

    blocked = links.step_up()
    links.api.post(f"/v1/agents/{links.agent_ids[0]}/disable", links.tenant)
    resolution = links.approve(blocked["step_up"]["consent_request_id"]).json()
    assert (resolution["status"], resolution["payable_until"]) == ("blocked", None)
    assert links.decision(blocked["decision_id"])["payable_until"] is None
    links.api.post(f"/v1/agents/{links.agent_ids[0]}/enable", links.tenant)

    expired = links.step_up()
    links.api.clock.advance(timedelta(minutes=11))
    after = links.decision(expired["decision_id"])
    assert (after["consent"]["status"], after["payable_until"]) == ("expired", None)


def test_each_window_comes_from_the_pack_that_granted_the_payment(
    links: Links, tmp_path: Path
) -> None:
    allow = links.ask().json()
    step_up = links.ask(amount=inr(6_000_00)).json()
    assert step_up["outcome"] == "step_up"
    links.api.use_packs(window_pack(tmp_path, "1.0.2", 300))
    links.api.use_settings(api_settings(links.api))

    resolution = links.approve(step_up["step_up"]["consent_request_id"]).json()

    # The allow keeps its own pack's 2 minutes; the approval was re-checked on 1.0.2.
    assert links.decision(allow["decision_id"])["payable_until"] == "2026-11-02T09:32:00Z"
    assert resolution["payable_until"] == "2026-11-02T09:35:00Z"
    assert links.decision(step_up["decision_id"])["payable_until"] == "2026-11-02T09:35:00Z"


def test_an_idempotent_replay_returns_the_same_payable_until(links: Links) -> None:
    first = links.ask(key="k-1").json()
    links.api.clock.advance(timedelta(minutes=1))

    again = links.ask(key="k-1").json()

    assert again["payable_until"] == first["payable_until"] == "2026-11-02T09:32:00Z"


def test_the_audit_record_never_carries_payable_until(links: Links) -> None:
    allow = links.ask().json()
    decision = links.step_up()
    resolution = links.approve(decision["step_up"]["consent_request_id"]).json()

    for audit_record_id in (allow["audit_record_id"], resolution["audit_record_id"]):
        assert b"payable_until" not in links.audit(audit_record_id).body
