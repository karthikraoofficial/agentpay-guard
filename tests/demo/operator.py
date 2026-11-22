"""Everything outside the demo: the worker (the mock settle sweep, the outbox relay and
the Evidence pack build) and the people the demo waits for (the Payer who opens a Consent
link and approves, and the reviewer who opens a Review link and signs off). Shared by the
demo CLI tests and the Scenario tests."""

import re
from datetime import timedelta

from adapters.memory.settling_rail import SettlingMockRail
from api.admin import create_tenant
from core.domain.disputes import DisputeId
from core.evidence import build_evidence_pack
from core.reconcile import sweep
from core.relay import relay_once
from tests.api.conftest import Api, tenant_webhooks
from tests.conftest import make_settings

SETTLE_AFTER = timedelta(seconds=60)
LINK = re.compile(r"https?://\S+#(\S+)")


class Operator:
    """Everything outside the CLI: the worker, the Payer and the reviewer."""

    def __init__(self, api: Api, out: list[str]) -> None:
        self.api = api
        self.out = out
        self.used: set[str] = set()
        self.built: set[str] = set()
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
        # The worker: mock settle, the outbox relay, and each new Dispute's pack.
        api.clock.advance(timedelta(seconds=30))
        sweep(api.store, SettlingMockRail(api.store), api.clock, SETTLE_AFTER)
        relay_once(api.store, api.workflow, api.clock, tenant_webhooks(api.store, api.clock))
        # Only this test's Tenants: on Postgres the relay also sees other tests' events.
        for dispute_id, tenant_id in api.workflow.dispute_starts.items():
            if dispute_id not in self.built and tenant_id in api.tenants:
                self.built.add(dispute_id)
                assert (
                    build_evidence_pack(
                        api.store, tenant_id, DisputeId(dispute_id), api.clock, api.packs, None
                    )
                    == "built"
                )


def demo_keys(api: Api) -> tuple[str, str]:
    """Serve with mock settle on, and make a Demo Tenant: its agent key and tenant key."""
    api.use_settings(make_settings(mock_settle_after_seconds=60))
    tenant_id, agent_key, tenant_key = create_tenant(api.store, "Demo", live=False, clock=api.clock)
    api.tenants.append(tenant_id)
    return agent_key, tenant_key
