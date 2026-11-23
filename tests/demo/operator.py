"""Everything outside the demo: the worker (the mock settle sweep, the outbox relay and
the Evidence pack build) and the people the demo waits for (the Payer who opens a Consent
link and approves, and the reviewer who opens a Review link and signs off). Shared by the
demo CLI tests and the Scenario tests."""

import re
from datetime import timedelta

from adapters.memory.rail import MockRail
from adapters.memory.settling_rail import SettlingMockRail
from api.admin import create_tenant
from core.debits import run_debit_step
from core.domain.disputes import DisputeId
from core.domain.parties import TenantId
from core.evidence import build_evidence_pack
from core.reconcile import sweep
from core.relay import relay_once
from tests.api.conftest import Api, tenant_webhooks
from tests.conftest import make_settings

SETTLE_AFTER = timedelta(seconds=60)
# The hosted demo's card-like debit (#280): booked now, debited after this notice period.
NOTICE = timedelta(seconds=120)
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
        # The worker: the debit step, mock settle, the outbox relay, and each new
        # Dispute's pack.
        api.clock.advance(timedelta(seconds=30))
        demo = api.client.app.state.settings.demo_controls_tenant_id  # type: ignore[attr-defined]
        demo_tenant = None if demo is None else TenantId(demo)
        run_debit_step(api.store, SettlingMockRail(api.store), api.clock, demo_tenant)
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
