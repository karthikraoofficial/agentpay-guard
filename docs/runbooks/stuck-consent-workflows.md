# Stuck consent workflows

A step_up's `step_up_consent` workflow isn't running: the worker or Temporal is down, or the workflow is stuck. Postgres owns consent state, and the workflow only notifies the Payer and writes the expiry (ADR-0009). So this is about lost notifications and late expiry records, not lost approvals.

## How you'd notice

- **Payers aren't notified.** The `consent_requested` event isn't delivered to start the workflow:

  ```bash
  psql -c "select event_type, count(*), min(occurred_at) as oldest, max(attempts) as most_attempts
           from outbox_events
           where delivered_at is null and not gave_up
             and event_type in ('consent_requested', 'dispute_opened', 'dispute_regenerate', 'dispute_closed')
           group by 1"
  ```

  Rows here older than a minute or two mean the relay can't start workflows.
- **Expired requests have no `expired` record.** These Consent requests are past expiry with no resolution:

  ```bash
  psql -c "select r.tenant_id, r.consent_request_id, r.expires_at
           from consent_requests r
           left join consent_resolutions s
             on s.tenant_id = r.tenant_id and s.consent_request_id = r.consent_request_id
           where s.consent_request_id is null and r.expires_at < now() - interval '5 minutes'"
  ```

- **The worker or Temporal is down:**

  ```bash
  $C ps worker temporal
  $C logs --since 10m worker | grep -E "polling|ERROR"    # healthy: "consent worker polling task queue agentpay-consent"
  $C exec -T temporal temporal operator cluster health --address localhost:7233   # SERVING
  ```

## What already happens

- **Approvals don't depend on the workflow.**
  - **Recording one:** a Payer's approval or decline is written to Postgres with its Audit record, then the workflow is signalled on a best-effort basis.
  - **If the worker or Temporal is down,** approvals still work.
- **Expiry can't be stretched.**
  - **How expiry is worked out:** when a Consent request is read, from its `expires_at`, set by the Policy pack's `consent_ttl_seconds` (600 s).
  - **After that time:** an approval is refused with `409 CONSENT_EXPIRED`, and `GET` shows `expired`, whatever the workflow is doing.
- **Workflows start late, not never.**
  - **Each pass:** the outbox relay keeps a `consent_requested` event and retries it until a workflow start succeeds.
  - **Starting twice is harmless:** the workflow id is the Consent request id, so a second start is refused.
  - **A workflow that starts after its expiry** sets no timer and writes its `expired` record at once.
- **Workers come and go.** Temporal resumes every waiting workflow on the next worker, so killing and restarting the worker is safe.
- **Long-running workflows can be normal.** A `dispute_pack` workflow waits until its Dispute's `respond_by` or until the Dispute closes, so it can run for days. List what's running:

  ```bash
  $C exec -T temporal temporal workflow list --address localhost:7233 --query 'ExecutionStatus="Running"'
  $C exec -T temporal temporal workflow describe --address localhost:7233 -w <workflow_id>
  ```

  A `step_up_consent` workflow should finish within its TTL plus a few minutes.

## What to do

1. **Restart what's down.** Run `$C up -d worker temporal`. The worker logs `consent worker polling task queue agentpay-consent` when it's ready. The relay then starts the waiting workflows within a few passes (`AGENTPAY_OUTBOX_POLL_INTERVAL_MS`), and each writes its notification or its `expired` record.
2. **Check one stuck request** with `temporal workflow describe -w <consent_request_id>`.
   - **`workflow not found`:** it was never started, so look at the outbox query above.
   - **`Running` past its expiry:** look for activity failures in `$C logs worker`.
3. **Don't write `expired` records by hand.** The workflow's expire activity writes nothing if the request was resolved meanwhile; a hand-written record could contradict a real approval.
4. **If Payers missed notifications while the worker was down,** the Tenant can send a fresh Consent link with `POST /v1/consent-requests/{id}/link`, as long as the request hasn't expired.

## When it's over

- Both queries above return no rows.
- The worker logs `polling`, and Temporal reports `SERVING`.
- `verify` is clean for the Tenants involved.
