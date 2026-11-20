# A rail outage

The rail (Razorpay, or the mock rail on the hosted demo) is down, slow, rate-limiting us, or refusing our keys.

## How you'd notice

- **The alerts (ADR-0026).** On the hosted demo, Grafana Cloud emails one of these. Thresholds are in `infra/grafana/alert-rules.yaml`.
  - **Rail: sweep lookups failing:** the reconciliation sweep reports errors on every run for 3 minutes.
  - **Rail: Payments or Refunds unsettled:** a Payment still scheduled, or a Refund still pending, past the sweep age for 10 minutes.
  - **Rail: circuit breaker open:** payments and refunds have answered `503 RAIL_UNAVAILABLE` for 5 minutes.

  The uptime check doesn't fire for this: `/healthz` doesn't call the rail.
- `POST /v1/payments` answers `202` with a Payment that stays `scheduled` with no `rail_payment_id`, and the API log has `rail unavailable for Payment …`. After a few of those in a row, payments and refunds answer `503 RAIL_UNAVAILABLE`, and the log has `rail breaker opened`:

  ```bash
  $C logs --since 1h api | grep -E "rail unavailable|rail breaker opened"
  ```
- `POST /v1/payments/{id}/refunds` answers `201` with a Refund that stays `pending`, and the log has `rail unavailable for Refund …`.
- The worker's sweep reports errors:

  ```bash
  $C logs --since 1h worker | grep -E "reconciliation sweep|debit step"
  # healthy:  reconciliation sweep checked 2 Payment(s), 0 error(s)
  ```

- Payments and Refunds pile up unsettled:

  ```bash
  psql -c "select tenant_id, payment_id, rail, rail_payment_id is not null as rail_answered, created_at
           from payments where status = 'scheduled' and created_at < now() - interval '10 minutes'
           order by created_at"
  psql -c "select tenant_id, refund_id, payment_id, created_at
           from refunds where status = 'pending' and created_at < now() - interval '10 minutes'"
  ```

  On Razorpay a Payment also waits out its Pre-debit notice, 36 h 5 min, as `scheduled` with a `debit_after` (ADR-0017). That is normal, not an outage.

## What already happens

- **Decisions don't change.** A Decision never calls the rail, so an outage can't turn any request into an allow it wouldn't otherwise get, and the decision path's latency is unaffected. An allow is permission; money only moves when its Payment is made.
- **Nothing is charged twice or late.**
  - Every rail call is keyed by our Payment or Refund id (ADR-0014, ADR-0024). A timeout leaves the Payment `scheduled` or the Refund `pending`, never `failed` by guess.
  - A Payment retried with the same Idempotency-Key asks the rail again. It finds the charge already made rather than making a second. A Refund retried with the same key returns the same `pending` Refund, and the sweep settles it.
- **The sweep finishes the job.** Every `AGENTPAY_RECONCILE_INTERVAL_SECONDS`, it asks the rail about Payments and Refunds still unsettled after `AGENTPAY_RECONCILE_AFTER_SECONDS` (10 minutes):
  - **The rail answers:** the answer is applied as its webhook would be.
  - **The rail has no record and never answered for it:** the Payment is failed with `RAIL_CHARGE_MISSING` and its Decision Voided, never charged late (ADR-0016). A Refund is failed with `RAIL_REFUND_MISSING`, so no money moved and the Tenant can ask again.
  - **The rail answered once but can't find it now:** that's treated as a lookup fault and retried, never failed.
  - **The rail errors:** that's logged and retried next interval.
- **Rail webhooks are retried by Razorpay.** Each is acted on at most once, so a burst after an outage settles each Payment once.
- **The circuit breaker fails fast (ADR-0025).**
  - **Opening:** after `AGENTPAY_RAIL_BREAKER_FAILURES` (5) unknown outcomes in a row, it opens. The API logs `rail breaker opened: payments and refunds answer 503 RAIL_UNAVAILABLE`.
  - **While open:** for `AGENTPAY_RAIL_BREAKER_OPEN_SECONDS` (30), `POST /v1/payments` and `POST /v1/payments/{id}/refunds` answer `503 RAIL_UNAVAILABLE` with `Retry-After` at once, and write nothing. Agents and Tenants retry after that, with the same Idempotency-Key.
  - **After the open period:** one request is let through to try the rail. An answer closes the breaker, and another timeout reopens it.
- **Keys:** a `401`/`403` from Razorpay is treated as "unknown" (`RailUnavailable`), not as a refusal, so bad keys stall Payments rather than failing them.

## What to do

1. **Find out which it is.**
   - **The rail itself:** check status.razorpay.com.
   - **Our keys:** Razorpay answers `401`/`403`. Check `AGENTPAY_RAZORPAY_KEY_ID` and `AGENTPAY_RAZORPAY_KEY_SECRET` in `.env`; only `rzp_test_` keys are accepted (ADR-0012).
   - **Rate limits:** Razorpay answers `429`. The sweep's interval sets the pace.
2. **Leave the Payments alone.** Don't mark them failed by hand: that skips the Audit record, the Void and the Tenant webhook. The sweep will settle them once the rail answers.
3. **If a Tenant asks:** a `scheduled` Payment may still be captured. To stop it, the Tenant waits for it to settle, then refunds it. A Payment that fails returns its Monthly cap through the Void.
4. **If the outage outlasts `AGENTPAY_RECONCILE_AFTER_SECONDS`:** Payments the rail never saw are failed by the sweep. Tell the Tenants affected to pay those Decisions again, if their Payment window is still open. Otherwise they need new Decisions.

## When it's over

- The two queries above return no rows, apart from Payments in their notice period.
- The sweep logs `0 error(s)`.
- `verify` is clean for the Tenants involved.
