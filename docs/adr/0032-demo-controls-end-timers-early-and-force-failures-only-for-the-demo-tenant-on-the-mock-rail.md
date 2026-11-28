# Demo controls end timers early and force failures only for the Demo Tenant on the mock rail

Several demo journeys end when a timer runs out: a Consent request nobody answers, a Payment window that closes, a Pre-debit notice period that ends. A visitor won't wait ten minutes to watch one. So the hosted demo has **Demo controls** (#280): a fast-forward button that ends one timer now, for one target.

A control never moves a clock. Shifting time would touch every deadline at once, and a Decision must replay byte-identically from its Audit record (ADR-0001). A control also never edits a Mandate, Decision or Audit record. Instead it appends one `demo_controls` row and one `demo_control` Audit record in the Tenant's chain, and each deadline check that honours a control reads **the real deadline or the control's time, whichever comes first**. A control can only make a deadline earlier.

The gate has three parts:

- `AGENTPAY_DEMO_CONTROLS_TENANT_ID` names the one Tenant. Settings refuse it with any rail but mock, so no control can touch real money. _(Updated: #300. A deployment can now reach several rails, so settings ask only that the mock rail be reachable, and each control that touches the rail refuses a Mandate or Payment on a real rail; see ADR-0033.)_
- The `/v1/demo/...` endpoints are mounted only when it is set. They need the `manage` scope and an idempotency key, and answer 404 for any other Tenant.
- Deadline checks read a control only through `core.demo_controls.control_for`, which answers None for any other Tenant, whatever the table holds. A test proves a non-Demo Tenant's deadlines ignore the table.

## How each timer ends

- **Consent request.** The control runs the same `expired` resolution the timer writes, with `resolved_via = demo_control`.
- **Payment window.** `payable_until` ends at the control's time, so paying is refused with `PAYMENT_WINDOW_CLOSED`. The Decision is not edited.
- **Pre-debit notice period.** The worker's debit step treats the Payment as due, then runs its usual re-check. It debits the Payment, or fails it if the Mandate no longer allows it (`MANDATE_REVOKED`). `AGENTPAY_MOCK_NOTICE_SECONDS` gives the mock rail a notice period, so the deployed demo behaves like a card debit.
- **Dispute deadline.** _(Added: #284)_ The control runs the transition the deadline timer runs, so the Dispute is `expired` and the Tenant gets `dispute.expired`. `respond_by` is not edited. The workflow is told to stop waiting, as for a Sign-off, and if its timer fires later anyway it finds the Dispute already closed.
- **Refund window.** _(Added: #285)_ A refund asked for at or after the control's time is refused with `REFUND_WINDOW_CLOSED`, as one after the Policy pack's Refund window would be (H2). The control is for a captured Payment on the mock rail only, and the Payment is not edited. Refunds aren't Decisions, so no replay depends on it.
- **Mock settle's delay (settle-now).** _(Added: #287)_ For a scheduled mock Payment, the worker's debit step treats its Pre-debit notice period as ended, as for the notice control. Once it is debited, the sweep settles it on its next pass through the settling mock rail, without waiting `AGENTPAY_MOCK_SETTLE_AFTER_SECONDS`. The capture is the same transition, Audit record and Tenant webhook as any other. The sweep reads the control only for the Demo Tenant, and a Payment that has settled answers 409 `PAYMENT_NOT_SCHEDULED`. A1, E1 and G1 press it, so the 90-second pitch doesn't wait two minutes for the bank in each.
- **Mandate expiry has no control.** Journey B10 grants a Mandate whose validity has already ended, so the real rule answers `MANDATE_EXPIRED` with no control and no engine change.

## The switches

The switches (#281) force a failure rather than end a timer. They use the same gate, and each writes its own `demo_controls` row and Audit record. `SwitchedRail` wraps the mock rail in the API and the worker and reads the same table, and every other Tenant's rail calls pass straight through.

- **AI off** (one Mandate). The Intent check answers `unknown` without calling the model, and a Dispute narrative falls back to the template.
- **Rail declining** (one Mandate). New charges and refunds answer failed.
- **Lose a payment** (one Mandate). New charges never reach the rail. The outcome is unknown, so the Payment stays scheduled until Reconciliation fails it with `RAIL_CHARGE_MISSING`. Forgetting a charge the rail had already answered for wouldn't work: Reconciliation rightly treats that as a lookup fault and retries (ADR-0016).
- **Rail down** (the Demo Tenant's rail calls). Rail calls raise `RailUnavailable` for 60 seconds, and the process-wide breaker opens at once (ADR-0025), so a payment from any Tenant on this API is refused while it is open. Because every visitor feels it, another outage can't start until 5 minutes after this one started, and a press during an outage joins it. Both lengths are in settings. A public `GET /v1/demo/status` reports the outage, and `/demo` shows every visitor a banner.
- **Tamper with a copy.** Read-only, so it writes no record. The caller names an Audit record, the API alters it in an in-memory copy of the chain, and the real Verify names the first broken record. We didn't have the caller supply the copy, because that would export other visitors' run inputs.

A switch runs before any Decision exists, so **a `demo_control` Audit record is the one kind that may have no `decision_id`**. It names its Mandate, or the rail, as its target. A database check enforces this for every other kind. Records written before this change keep their exact bytes and hashes. Once a switch record exists, migration 0026 can't be downgraded, because an append-only record can't be given a Decision.

## Consequences

- **Replay is unaffected.** The engine never sees a control. Decisions before and after one replay byte-identically, and a test proves it per control.
- **The Audit chain shows every control.** `verify` covers `demo_control` records like any others.
- **Evidence bundles leave `demo_control` records out.** They end demo timers and are not evidence about the payment. This is a known limit: a bundle for a Demo Tenant Payment doesn't show that a timer was ended early. _(Updated: #284. The Dispute-deadline control doesn't change this. Its record comes after the `dispute_opened` record, where the bundle stops, and an expired Dispute is never built again. So the limit still applies only to the earlier timers, and it stays.)_
- **Later controls follow the same rule.** The Intent check's usable age is #294, and its control id must go in the Decision's Audit record because it affects a Decision. The Dispute deadline is #284, the Refund window #285, and settle-now #287.
