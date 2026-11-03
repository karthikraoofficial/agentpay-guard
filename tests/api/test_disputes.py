"""/v1/disputes: a Tenant reports that the Payer has disputed a captured Payment, and
reads the Evidence pack built for it (ADR-0018).

The `api` fixture verifies every chain and replays every Decision when each test ends,
so `dispute_opened` records must keep the chain intact."""

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from api.admin import replay_record
from core.domain.canonical_json import canonical_json
from core.domain.disputes import DisputeId
from core.domain.evidence import Claim, EvidencePack
from core.evidence import build_evidence_pack, gather_bundle
from core.relay import relay_once
from tests.api.conftest import NOW, Api, tenant_webhooks
from tests.api.test_rail_webhooks import SECRET, Webhooks, event
from tests.api.test_tenants_payers_agents import assert_problem
from tests.conftest import make_settings
from tests.packs import phase_1_packs

REVIEWER = {"id": "ops-7", "name": "Meera Iyer"}
RESPOND_BY = (NOW + timedelta(days=10)).isoformat().replace("+00:00", "Z")


class Disputes(Webhooks):
    """A Tenant with a manage key, whose Payments can be captured by rail webhook."""

    def __init__(self, api: Api, name: str = "acme") -> None:
        super().__init__(api, name)
        self.manage = api.new_key(self.tenant_id(), {"manage"})

    def captured(self, **overrides: Any) -> dict[str, Any]:
        payment: dict[str, Any] = self.pay(self.allow(**overrides)["decision_id"]).json()
        self.deliver(event(payment), f"evt_{payment['payment_id']}")
        assert self.status(payment) == "captured"
        return payment

    def open(self, payment_id: str, *, headers=None, key: str | None = None, **body: Any):  # type: ignore[no-untyped-def]
        sent = {"payment_id": payment_id, "reason": "unauthorised", "respond_by": RESPOND_BY}
        sent |= body
        headers = headers or self.manage
        if key is None:
            return self.api.post("/v1/disputes", headers, sent)
        return self.api.client.post(
            "/v1/disputes", headers={**headers, "Idempotency-Key": key}, json=sent
        )

    def get(self, dispute_id: str, headers=None):  # type: ignore[no-untyped-def]
        return self.api.client.get(f"/v1/disputes/{dispute_id}", headers=headers or self.manage)

    def sign_off(self, dispute_id: str, *, headers=None, key: str | None = None, **body: Any):  # type: ignore[no-untyped-def]
        sent: dict[str, Any] = {"pack_version": 1, "reviewer": REVIEWER} | body
        path = f"/v1/disputes/{dispute_id}/sign-off"
        headers = headers or self.manage
        if key is None:
            return self.api.post(path, headers, sent)
        return self.api.client.post(path, headers={**headers, "Idempotency-Key": key}, json=sent)

    def relay(self) -> None:
        api = self.api
        relay_once(api.store, api.workflow, api.clock, tenant_webhooks(api.store, api.clock))

    def build(self, dispute_id: str) -> str:
        """What the workflow's build activity does, run here without Temporal."""
        return build_evidence_pack(
            self.api.store, self.tenant_id(), DisputeId(dispute_id), self.api.clock
        )

    def built(self, payment: dict[str, Any]) -> dict[str, Any]:
        dispute_id: str = self.open(payment["payment_id"]).json()["dispute_id"]
        assert self.build(dispute_id) == "built"
        body: dict[str, Any] = self.get(dispute_id).json()
        return body


@pytest.fixture
def disputes(api: Api, tmp_path: Path) -> Disputes:
    api.use_packs(phase_1_packs(tmp_path))
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    return Disputes(api)


# --- Opening a Dispute ------------------------------------------------------------------


def test_a_captured_payment_can_be_disputed(disputes: Disputes) -> None:
    payment = disputes.captured()

    response = disputes.open(payment["payment_id"], payer_statement="I never asked for this")

    assert response.status_code == 201, response.text
    dispute = response.json()
    assert dispute["dispute_id"].startswith("dsp_")
    assert (dispute["payment_id"], dispute["decision_id"]) == (
        payment["payment_id"],
        payment["decision_id"],
    )
    assert (dispute["reason"], dispute["status"]) == ("unauthorised", "open")
    assert dispute["respond_by"] == RESPOND_BY
    assert dispute["pack"] is None
    assert "payer_statement" not in dispute


def test_opening_a_dispute_writes_a_dispute_opened_record_without_the_statement(
    disputes: Disputes,
) -> None:
    payment = disputes.captured()

    dispute = disputes.open(payment["payment_id"], payer_statement="I never asked").json()

    record = disputes.chain()[-1]
    assert (record.kind, record.decision_id) == ("dispute_opened", payment["decision_id"])
    assert record.dispute.dispute_id == dispute["dispute_id"]
    assert (record.dispute.payment_id, record.dispute.reason) == (
        payment["payment_id"],
        "unauthorised",
    )
    # sha256 of the UTF-8 text "I never asked"
    assert record.dispute.payer_statement_hash == (
        "sha256:b936d714d92f0def62389f9c8e1af1220dc165a78e59e6d8bd80b7c165c582b9"
    )
    assert "I never asked" not in record.model_dump_json()


def test_replay_names_a_dispute_record_and_finds_nothing_to_re_run(disputes: Disputes) -> None:
    disputes.open(disputes.captured()["payment_id"])
    record = disputes.chain()[-1]
    messages: list[str] = []

    code = replay_record(
        disputes.api.store,
        disputes.tenant_id(),
        record.audit_record_id,
        disputes.api.packs,
        messages.append,
    )

    assert code == 0
    assert "is a Dispute record with no engine run" in messages[0]


def test_opening_a_dispute_leaves_the_payment_and_spend_alone(disputes: Disputes) -> None:
    payment = disputes.captured()
    before = disputes.payment(payment["payment_id"]).json()

    disputes.open(payment["payment_id"])

    assert disputes.payment(payment["payment_id"]).json() == before
    # Spent this month only falls by a Void, and none is written.
    assert [r.kind for r in disputes.chain()].count("void") == 0


def test_only_a_captured_payment_can_be_disputed(disputes: Disputes) -> None:
    scheduled = disputes.paid()

    assert_problem(disputes.open(scheduled["payment_id"]), 409, "PAYMENT_NOT_DISPUTABLE")


def test_a_payment_is_disputed_only_once(disputes: Disputes) -> None:
    payment = disputes.captured()
    disputes.open(payment["payment_id"])

    assert_problem(disputes.open(payment["payment_id"]), 409, "PAYMENT_ALREADY_DISPUTED")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"respond_by": "2026-11-02T09:29:00Z"}, "RESPOND_BY_PASSED"),
        ({"reason": "changed_my_mind"}, "VALIDATION_FAILED"),
        ({"payer_statement": ""}, "VALIDATION_FAILED"),
    ],
)
def test_a_bad_dispute_is_refused(disputes: Disputes, body: dict[str, Any], code: str) -> None:
    payment = disputes.captured()

    assert_problem(disputes.open(payment["payment_id"], **body), 422, code)


def test_disputes_need_a_manage_key(disputes: Disputes) -> None:
    payment = disputes.captured()
    opened = disputes.open(payment["payment_id"]).json()

    for key in (disputes.decide, disputes.consent):
        assert_problem(disputes.open(payment["payment_id"], headers=key), 403, "INSUFFICIENT_SCOPE")
        assert_problem(disputes.get(opened["dispute_id"], key), 403, "INSUFFICIENT_SCOPE")


def test_another_tenants_payment_or_dispute_is_not_found(disputes: Disputes) -> None:
    payment = disputes.captured()
    opened = disputes.open(payment["payment_id"]).json()
    other = Disputes(disputes.api, "globex")

    assert_problem(other.open(payment["payment_id"]), 404, "PAYMENT_NOT_FOUND")
    assert_problem(other.get(opened["dispute_id"]), 404, "DISPUTE_NOT_FOUND")


def test_a_retry_with_the_same_key_opens_one_dispute(disputes: Disputes) -> None:
    payment = disputes.captured()

    first = disputes.open(payment["payment_id"], key="dsp-1")
    again = disputes.open(payment["payment_id"], key="dsp-1")

    assert (first.status_code, again.status_code) == (201, 201)
    assert first.json() == again.json()
    assert [r.kind for r in disputes.chain()].count("dispute_opened") == 1


# --- Building the Evidence pack -------------------------------------------------------


def test_the_outbox_starts_one_pack_workflow_per_dispute(disputes: Disputes) -> None:
    payment = disputes.captured()
    dispute = disputes.open(payment["payment_id"]).json()

    disputes.relay()
    disputes.relay()

    # The Postgres test database is shared: other tests' events may be relayed too.
    starts = disputes.api.workflow.dispute_starts
    ours = [d for d, tenant in starts.items() if tenant == disputes.tenant_id()]
    assert ours == [dispute["dispute_id"]]


def test_a_built_pack_puts_the_dispute_in_review(disputes: Disputes) -> None:
    payment = disputes.captured()

    dispute = disputes.built(payment)

    assert dispute["status"] == "in_review"
    pack = dispute["pack"]
    assert (pack["version"], pack["narrative_source"]) == (1, "template")
    assert pack["bundle_hash"].startswith("sha256:")
    trail = pack["bundle"]["trail"]
    assert [r["kind"] for r in trail] == ["decision", "payment", "payment", "dispute_opened"]
    assert [r["record"]["payment"]["status"] for r in trail[1:3]] == ["scheduled", "captured"]
    assert pack["bundle"]["mandate"]["mandate_id"] == disputes.mandate_id
    assert [e["status"] for e in pack["bundle"]["rail_events"]] == ["captured"]
    assert pack["bundle"]["consent"] is None


def test_every_sentence_of_the_template_narrative_cites_the_trail(disputes: Disputes) -> None:
    payment = disputes.captured()

    pack = disputes.built(payment)["pack"]

    in_bundle = {r["audit_record_id"] for r in pack["bundle"]["trail"]}
    assert pack["narrative"]
    for claim in pack["narrative"]:
        assert claim["cites"] and set(claim["cites"]) <= in_bundle, claim
    cited = {c for claim in pack["narrative"] for c in claim["cites"]}
    assert cited == in_bundle  # nothing on the trail goes unmentioned
    text = " ".join(claim["text"] for claim in pack["narrative"])
    assert "INR 1,250.00" in text and "FreshMart" in text and "captured" in text


def test_an_approved_step_up_brings_its_consent_into_the_pack(disputes: Disputes) -> None:
    decision = disputes.step_up()
    csr = decision["step_up"]["consent_request_id"]
    assert disputes.approve(csr).status_code == 200
    payment = disputes.pay(decision["decision_id"]).json()
    disputes.deliver(event(payment), "evt_step_up")

    pack = disputes.built(payment)["pack"]

    assert pack["bundle"]["consent"]["consent_request_id"] == csr
    kinds = [r["kind"] for r in pack["bundle"]["trail"]]
    assert kinds[:2] == ["decision", "consent_resolution"]
    approval = next(c for c in pack["narrative"] if "approved" in c["text"])
    resolution_id = pack["bundle"]["trail"][1]["audit_record_id"]
    assert resolution_id in approval["cites"]


def test_earlier_decisions_by_the_agent_at_the_merchant_are_context(disputes: Disputes) -> None:
    blocked = disputes.ask(amount={"minor": 20_000_00, "currency": "INR"}).json()
    assert blocked["outcome"] == "block"
    elsewhere = disputes.ask(merchant={"id": "m_2", "name": "Other", "mcc": "5411"}).json()
    payment = disputes.captured()

    pack = disputes.built(payment)["pack"]

    related = [r["record"]["decision_id"] for r in pack["bundle"]["related"]]
    assert related == [blocked["decision_id"]]
    assert elsewhere["decision_id"] not in related
    mention = next(c for c in pack["narrative"] if blocked["audit_record_id"] in c["cites"])
    assert "block" in mention["text"]


def test_the_bundle_is_the_same_however_the_chain_grows(disputes: Disputes) -> None:
    payment = disputes.captured()
    first = disputes.built(payment)["pack"]
    disputes.captured()  # more records after the Dispute was opened

    with disputes.api.store.transaction(disputes.tenant_id()) as tx:
        dispute = tx.get_dispute(DisputeId(first["dispute_id"]))
        assert dispute is not None
        again = gather_bundle(tx, dispute)

    assert again.hash() == first["bundle_hash"]


def test_building_twice_keeps_one_pack(disputes: Disputes) -> None:
    payment = disputes.captured()
    dispute = disputes.built(payment)

    assert disputes.build(dispute["dispute_id"]) == "already_built"
    assert disputes.get(dispute["dispute_id"]).json() == dispute


def test_a_dispute_another_tenant_owns_is_not_built(disputes: Disputes) -> None:
    payment = disputes.captured()
    dispute = disputes.open(payment["payment_id"]).json()
    other = Disputes(disputes.api, "globex")

    assert other.build(dispute["dispute_id"]) == "missing"


# --- Sign-off ---------------------------------------------------------------------------


def test_signing_off_the_current_pack_makes_the_dispute_ready(disputes: Disputes) -> None:
    dispute = disputes.built(disputes.captured())
    disputes.api.clock.advance(timedelta(hours=2))

    response = disputes.sign_off(dispute["dispute_id"])

    assert response.status_code == 200, response.text
    ready = response.json()
    assert ready["status"] == "ready"
    assert ready["sign_off"] == {
        "reviewer": REVIEWER,
        "pack_version": 1,
        "pack_hash": dispute["pack_hash"],
        "signed_off_at": "2026-11-02T11:30:00Z",
    }
    assert disputes.get(dispute["dispute_id"]).json() == ready


def test_the_pack_hash_is_the_hash_of_the_pack_as_shown(disputes: Disputes) -> None:
    dispute = disputes.built(disputes.captured())

    shown = EvidencePack.model_validate(dispute["pack"])

    assert dispute["pack_hash"] == "sha256:" + hashlib.sha256(canonical_json(shown)).hexdigest()


def test_sign_off_writes_the_pack_hash_into_the_audit_chain(disputes: Disputes) -> None:
    dispute = disputes.built(disputes.captured())

    disputes.sign_off(dispute["dispute_id"])

    record = disputes.chain()[-1]
    assert (record.kind, record.decision_id) == ("dispute_signed_off", dispute["decision_id"])
    assert record.sign_off.model_dump() == {
        "dispute_id": dispute["dispute_id"],
        "pack_version": 1,
        "pack_hash": dispute["pack_hash"],
        "reviewer": REVIEWER,
    }


def test_a_pack_version_that_isnt_current_cannot_be_signed(disputes: Disputes) -> None:
    dispute = disputes.built(disputes.captured())

    assert_problem(
        disputes.sign_off(dispute["dispute_id"], pack_version=2), 409, "PACK_VERSION_NOT_CURRENT"
    )


def test_a_dispute_whose_pack_isnt_built_cannot_be_signed(disputes: Disputes) -> None:
    dispute = disputes.open(disputes.captured()["payment_id"]).json()

    assert_problem(disputes.sign_off(dispute["dispute_id"]), 409, "DISPUTE_NOT_IN_REVIEW")


def test_a_signed_off_dispute_cannot_be_signed_again(disputes: Disputes) -> None:
    dispute = disputes.built(disputes.captured())
    disputes.sign_off(dispute["dispute_id"])

    again = disputes.sign_off(dispute["dispute_id"], reviewer={"id": "ops-8", "name": "Ravi"})

    assert_problem(again, 409, "DISPUTE_NOT_IN_REVIEW")
    assert [r.kind for r in disputes.chain()].count("dispute_signed_off") == 1


def test_a_retry_with_the_same_key_signs_off_once(disputes: Disputes) -> None:
    dispute = disputes.built(disputes.captured())

    first = disputes.sign_off(dispute["dispute_id"], key="so-1")
    again = disputes.sign_off(dispute["dispute_id"], key="so-1")

    assert (first.status_code, again.status_code) == (200, 200)
    assert first.json() == again.json()
    assert [r.kind for r in disputes.chain()].count("dispute_signed_off") == 1


@pytest.mark.parametrize(
    "reviewer", [{"id": "", "name": "Meera"}, {"id": "ops-7"}, {"id": "ops-7", "name": " "}]
)
def test_sign_off_needs_a_named_reviewer(disputes: Disputes, reviewer: dict[str, str]) -> None:
    dispute = disputes.built(disputes.captured())

    assert_problem(
        disputes.sign_off(dispute["dispute_id"], reviewer=reviewer), 422, "VALIDATION_FAILED"
    )


def test_sign_off_needs_a_manage_key_and_the_tenants_dispute(disputes: Disputes) -> None:
    dispute = disputes.built(disputes.captured())
    other = Disputes(disputes.api, "globex")

    for key in (disputes.decide, disputes.consent):
        assert_problem(
            disputes.sign_off(dispute["dispute_id"], headers=key), 403, "INSUFFICIENT_SCOPE"
        )
    assert_problem(other.sign_off(dispute["dispute_id"]), 404, "DISPUTE_NOT_FOUND")


def test_a_stored_pack_that_differs_from_the_signed_hash_is_detectable(disputes: Disputes) -> None:
    dispute = disputes.built(disputes.captured())
    disputes.sign_off(dispute["dispute_id"])
    signed = disputes.chain()[-1].sign_off.pack_hash

    tampered = EvidencePack.model_validate(dispute["pack"]).model_copy(
        update={
            "narrative": (
                Claim(
                    text="The Payer asked for this.",
                    cites=(dispute["pack"]["narrative"][0]["cites"][0],),
                ),
            )
        }
    )

    assert tampered.hash() != signed
    assert (
        EvidencePack.model_validate(disputes.get(dispute["dispute_id"]).json()["pack"]).hash()
        == signed
    )
