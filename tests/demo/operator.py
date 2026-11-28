"""Everything outside the demo: the worker (the mock settle sweep, the outbox relay and
the Evidence pack build) and the people the demo waits for (the Payer who opens a Consent
link and approves, and the reviewer who opens a Review link and signs off). Shared by the
demo CLI tests and the Scenario tests."""

import re
from collections.abc import Mapping
from datetime import timedelta

from adapters.memory.rail import MockRail
from adapters.memory.settling_rail import SettlingMockRail
from api.admin import create_tenant
from core.debits import run_debit_step
from core.demo_switches import switchable_writer
from core.domain.disputes import DisputeId
from core.domain.evidence import EvidenceBundle
from core.domain.parties import TenantId
from core.evidence import ModelNarrative, NarrativeWriter, build_evidence_pack, template_narrative
from core.ports import WebhookSender
from core.reconcile import sweep
from core.relay import relay_once
from demo.webhook_inbox import BadSignature, WebhookInbox
from tests.api.conftest import Api, tenant_webhooks
from tests.conftest import make_settings

SETTLE_AFTER = timedelta(seconds=60)
# The hosted demo's card-like debit (#280): booked now, debited after this notice period.
NOTICE = timedelta(seconds=120)
LINK = re.compile(r"https?://\S+#(\S+)")
# Where the Demo Tenant's endpoint points in tests: the Demo runner's receiver.
RECEIVER = "https://demo-runner.test/webhooks"


class InboxSender:
    """Delivers each Tenant webhook straight into a webhook inbox, answering as the Demo
    runner's receiver does: 200 for a delivery that verifies, 400 for one that doesn't."""

    def __init__(self, inbox: WebhookInbox) -> None:
        self.inbox = inbox

    def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> int:
        try:
            self.inbox.receive(body, headers)
        except BadSignature:
            return 400
        return 200


def register_receiver(api: Api, tenant_key: str) -> str:
    """Point the Demo Tenant's endpoint at the receiver, as the provision wizard does, and
    return the signing secret the API shows that once."""
    registered = api.client.post(
        "/v1/webhook-endpoint",
        headers={"Authorization": f"Bearer {tenant_key}", "Idempotency-Key": "inbox-endpoint"},
        json={"url": RECEIVER},
    )
    assert registered.status_code == 201, registered.text
    secret: str = registered.json()["secret"]
    return secret


def webhook_inbox(api: Api, tenant_key: str) -> WebhookInbox:
    """An inbox holding the secret of the Demo Tenant's newly registered endpoint."""
    return WebhookInbox(register_receiver(api, tenant_key), now=api.clock.now)


def stand_in_writer(tenant_id: TenantId, bundle: EvidenceBundle) -> ModelNarrative:
    """The model's place in CI: a narrative whose claims cite the bundle's own records,
    which no scripted reply can, since record ids are made at run time."""
    return ModelNarrative(template_narrative(bundle), ())


class Operator:
    """Everything outside the CLI: the worker, the Payer and the reviewer."""

    def __init__(self, api: Api, out: list[str], sender: WebhookSender | None = None) -> None:
        self.api = api
        self.out = out
        self.sender = sender
        self.used: set[str] = set()
        self.waits = 0

    def _links(self, label: str) -> list[str]:
        tokens = []
        for line in self.out:
            match = LINK.search(line)
            if label in line and match and match.group(1) not in self.used:
                tokens.append(match.group(1))
        return tokens

    def wait(self) -> None:
        self.waits += 1
        api = self.api
        for token in self._links("Consent link"):
            self.used.add(token)
            approved = api.client.post(
                "/v1/consent-link/approve",
                headers={"Consent-Link": token, "Idempotency-Key": f"tap-{token[:8]}"},
            )
            assert approved.status_code == 200, approved.text
        for token in self._links("Review link"):
            self.used.add(token)
            signed = api.client.post(
                "/v1/review-link/sign-off",
                headers={"Review-Link": token, "Idempotency-Key": f"so-{token[:8]}"},
                json={"pack_version": 1},
            )
            assert signed.status_code == 200, signed.text
        # The worker: the debit step, mock settle, the outbox relay, and each Dispute's
        # next pack (its first, or one a Regenerate asked for).
        api.clock.advance(timedelta(seconds=30))
        demo = api.client.app.state.settings.demo_controls_tenant_id  # type: ignore[attr-defined]
        demo_tenant = None if demo is None else TenantId(demo)
        run_debit_step(api.store, SettlingMockRail(api.store), api.clock, demo_tenant)
        sweep(
            api.store, SettlingMockRail(api.store), api.clock, SETTLE_AFTER, demo_tenant=demo_tenant
        )
        relay_once(
            api.store, api.workflow, api.clock, tenant_webhooks(api.store, api.clock, self.sender)
        )
        # As the worker does, the AI-off Demo control keeps the model out (ADR-0032).
        writer: NarrativeWriter = stand_in_writer
        if demo_tenant is not None:
            writer = switchable_writer(stand_in_writer, api.store, demo_tenant)
        # Only this test's Tenants: on Postgres the relay also sees other tests' events.
        for dispute_id, tenant_id in api.workflow.dispute_starts.items():
            if tenant_id in api.tenants:
                built = build_evidence_pack(
                    api.store, tenant_id, DisputeId(dispute_id), api.clock, api.packs, writer
                )
                assert built in ("built", "already_built", "closed"), built


def demo_keys(api: Api) -> tuple[str, str]:
    """Serve as the hosted demo does (mock settle, the mock notice period, Demo controls
    for this Tenant), and make the Demo Tenant: its agent key and tenant key."""
    tenant_id, agent_key, tenant_key = create_tenant(api.store, "Demo", live=False, clock=api.clock)
    api.tenants.append(tenant_id)
    api.rail = MockRail(
        notice=NOTICE, clock=api.clock, debits=SettlingMockRail(api.store).get_charge
    )
    api.use_settings(
        make_settings(
            mock_settle_after_seconds=int(SETTLE_AFTER.total_seconds()),
            mock_notice_seconds=int(NOTICE.total_seconds()),
            demo_controls_tenant_id=tenant_id,
        )
    )
    return agent_key, tenant_key
