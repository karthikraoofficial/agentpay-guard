# Demo controls end timers early and force failures only for the Demo Tenant on the mock rail

Several demo journeys end when a timer runs out: a Consent request nobody answers, a Payment window that closes, a Pre-debit notice period that ends. A visitor won't wait ten minutes to watch one. So the hosted demo has **Demo controls** (#280): a fast-forward button that ends one timer now, for one target.

A control never moves a clock. Shifting time would touch every deadline at once, and a Decision must replay byte-identically from its Audit record (ADR-0001). A control also never edits a Mandate, Decision or Audit record. Instead it appends one `demo_controls` row and one `demo_control` Audit record in the Tenant's chain, and each deadline check that honours a control reads **the real deadline or the control's time, whichever comes first**. A control can only make a deadline earlier.

The gate has three parts:

- `AGENTPAY_DEMO_CONTROLS_TENANT_ID` names the one Tenant. Settings refuse it with any rail but mock, so no control can touch real money.
- The `/v1/demo/...` endpoints are mounted only when it is set. They need the `manage` scope and an idempotency key, and answer 404 for any other Tenant.
- Deadline checks read a control only through `core.demo_controls.control_for`, which answers None for any other Tenant, whatever the table holds. A test proves a non-Demo Tenant's deadlines ignore the table.

## How each timer ends

- **Consent request.** The control runs the same `expired` resolution the timer writes, with `resolved_via = demo_control`.
- **Payment window.** `payable_until` ends at the control's time, so paying is refused with `PAYMENT_WINDOW_CLOSED`. The Decision is not edited.
- **Pre-debit notice period.** The worker's debit step treats the Payment as due, then runs its usual re-check. It debits the Payment, or fails it if the Mandate no longer allows it (`MANDATE_REVOKED`). `AGENTPAY_MOCK_NOTICE_SECONDS` gives the mock rail a notice period, so the deployed demo behaves like a card debit.
- **Mandate expiry has no control.** Journey B10 grants a Mandate whose validity has already ended, so the real rule answers `MANDATE_EXPIRED` with no control and no engine change.

## Consequences

- **Replay is unaffected.** The engine never sees a control. Decisions before and after one replay byte-identically, and a test proves it per control.
- **The Audit chain shows every control.** `verify` covers `demo_control` records like any others.
- **Evidence bundles leave `demo_control` records out.** They end demo timers and are not evidence about the payment. This is a known limit: a bundle for a Demo Tenant Payment doesn't show that a timer was ended early. #284 adds the Dispute-deadline control and revisits it.
- **Later controls follow the same rule.** The Intent check's usable age is #294, and its control id must go in the Decision's Audit record because it affects a Decision. The Dispute deadline is #284 and settle-now is #287.
