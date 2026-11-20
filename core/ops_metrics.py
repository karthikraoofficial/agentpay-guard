"""The metrics an operator is alerted on (#228, ADR-0026), one or more per runbook symptom.

Every name here is queried by an alert rule in `infra/grafana/alert-rules.json`. Their
attributes are identifiers and codes only, never names, amounts or free text. Gauges are
exported as last set until set again, so one that stops being set (its process down)
goes stale, and a rule can alert on that as no data.
"""

from datetime import datetime, timedelta
from typing import Protocol

from core.ports import ConsentBacklog, EventBus, Telemetry
from core.reconcile import SweepResult
from core.relay import WORKFLOW_EVENT_TYPES

# Rail outage (docs/runbooks/rail-outage.md)
SWEEP_CHECKED = "agentpay.sweep.checked"  # gauge: Payments and Refunds the last sweep asked about
SWEEP_ERRORS = "agentpay.sweep.errors"  # gauge: of those, the lookups that failed
PAYMENTS_UNSETTLED = "agentpay.payments.unsettled"  # gauge: scheduled past the sweep age
REFUNDS_UNSETTLED = "agentpay.refunds.unsettled"  # gauge: pending past the sweep age
RAIL_BREAKER_OPEN = "agentpay.rail.breaker_open"  # gauge: 1 while the API refuses rail calls

# Model outage (docs/runbooks/model-outage.md)
INTENT_VERDICTS = "agentpay.intent.verdicts"  # counter: by verdict and failure_cause
NARRATIVE_SOURCE = "agentpay.narrative.source"  # counter: by source, and fallback
MODEL_CALLS = "agentpay.model.calls"  # counter: by purpose and outcome (counted or capped)

# Stuck consent workflows (docs/runbooks/stuck-consent-workflows.md)
OUTBOX_UNDELIVERED = "agentpay.outbox.undelivered"  # gauge: workflow events, by age
CONSENT_EXPIRED_UNRESOLVED = "agentpay.consent.expired_unresolved"  # gauge

# Decisions
DECISION_DURATION = "agentpay.decision.duration"  # histogram, milliseconds, by outcome

# The outbox's age buckets, and how late an expired Consent request counts as stuck: the
# same ages as the runbooks' queries.
OUTBOX_AGES = (("under_1m", timedelta(minutes=1)), ("1m_to_10m", timedelta(minutes=10)))
OUTBOX_OLDEST = "over_10m"
EXPIRY_GRACE = timedelta(minutes=5)


def report_sweep(telemetry: Telemetry, sweep: SweepResult) -> None:
    telemetry.set_gauge(SWEEP_CHECKED, sweep.checked, {})
    telemetry.set_gauge(SWEEP_ERRORS, sweep.errors, {})
    telemetry.set_gauge(PAYMENTS_UNSETTLED, sweep.unsettled_payments, {})
    telemetry.set_gauge(REFUNDS_UNSETTLED, sweep.unsettled_refunds, {})


def report_breaker(telemetry: Telemetry, is_open: bool) -> None:
    telemetry.set_gauge(RAIL_BREAKER_OPEN, 1 if is_open else 0, {})


def outbox_by_age(occurred: list[datetime], now: datetime) -> dict[str, int]:
    """How many undelivered events fall in each age bucket, every bucket present."""
    counts = {name: 0 for name, _ in OUTBOX_AGES} | {OUTBOX_OLDEST: 0}
    for at in occurred:
        age = now - at
        name = next((n for n, limit in OUTBOX_AGES if age < limit), OUTBOX_OLDEST)
        counts[name] += 1
    return counts


class BacklogStore(EventBus, ConsentBacklog, Protocol):
    """The outbox and the Consent requests, read across every Tenant."""


# Enough to tell a stuck relay from a busy one; past this, the oldest are what count.
BACKLOG_LIMIT = 1000


def report_backlog_once(store: BacklogStore, telemetry: Telemetry, now: datetime) -> None:
    """Workflow events still undelivered (whether due now or put off), by age, and Consent
    requests more than EXPIRY_GRACE past expiry with no resolution."""
    waiting = store.undelivered(BACKLOG_LIMIT, None, WORKFLOW_EVENT_TYPES)
    for age, count in outbox_by_age([e.occurred_at for e in waiting], now).items():
        telemetry.set_gauge(OUTBOX_UNDELIVERED, count, {"age": age})
    late = store.expired_unresolved_consents(now - EXPIRY_GRACE)
    telemetry.set_gauge(CONSENT_EXPIRED_UNRESOLVED, late, {})
