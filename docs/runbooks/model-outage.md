# A model outage

The Claude API is down, slow or erroring, or a Tenant has reached its Model spend cap (ADR-0020). Two features call a model, both through ModelGateway:
- **The Intent check:** `claude-haiku-5-5` by default, with a 2.5 s timeout.
- **The dispute narrative:** `claude-sonnet-5-5` by default, with a 45 s timeout.

## How you'd notice

- **The alerts (ADR-0026).** On the hosted demo, Grafana Cloud emails one of these. Thresholds are in `infra/grafana/alert-rules.yaml`.
  - **Model: Intent checks unknown:** over 15 minutes, more than 20% of Intent checks that called the model came back `unknown` for `timeout`, `error` or `unparseable`. The share is taken over at least 5 checks, so one unknown check among a few doesn't fire it.
  - **Model: dispute narratives written by code:** in the last hour, an Evidence pack fell back to the code-written narrative because the model call failed.
  - **Model: Model spend cap reached:** in the last 15 minutes, a Tenant's model call was refused by its daily cap. The email names the Tenant and the purpose.

  The uptime check doesn't fire for this: `/healthz` doesn't call a model, and Decisions keep answering, with step-ups.
- **More step-ups.** Decisions from the medium tier up start stepping up with `INTENT_UNKNOWN`.
- **Unknown Intent checks.** `POST /v1/intents` answers with verdict `unknown` and a `failure_cause`:

  ```bash
  psql -c "select verdict, coalesce(failure_cause, '-') as cause, count(*)
           from intent_checks where created_at > now() - interval '24 hours'
           group by 1, 2 order by 3 desc"
  # causes: timeout, error, capped (the spend cap), unparseable, disabled
  ```

- **Code-written narratives.** New Evidence packs say `narrative_source: template` and `narrative_note: model narrative unavailable`, and `narrative_attempts` shows the failed calls.
- **Traces and logs.** Langfuse shows `model_gateway.complete` spans with errors or long latency. Each call logs its model, prompt version, tokens, cost and latency (non-negotiable 10).
- **The spend cap reached.** Today's calls per Tenant and purpose, against `AGENTPAY_INTENT_CHECK_DAILY_CAP` and `AGENTPAY_DISPUTE_NARRATIVE_DAILY_CAP`:

  ```bash
  psql -c "select tenant_id, purpose, calls from model_calls where day = current_date"
  ```

## What already happens

- **Never an allow on a guess.** A model's answer is only a Signal (non-negotiable 1, ADR-0006).
  - **What becomes `unknown`:** a timeout, an error, an answer that can't be read, or a reached cap.
  - **What `unknown` does:** from the medium tier up, it's a step_up with `INTENT_UNKNOWN`. Low-tier payments don't need an Intent check and are unaffected.
- **The Payer decides instead.** A step_up asks the Payer through a Consent request, so the payment can still go ahead with their approval.
- **Narratives fall back.** If the model call fails, or its narrative fails the citation checker twice, the narrative is written by code from the Evidence bundle and flagged. The pack is still built, assessed and ready for Sign-off.
- **Spend is bounded.** Past a Tenant's daily cap, the model isn't called at all, and the caller fails closed as above.

## What to do

1. **Check whether it's the provider.** Look at status.anthropic.com, and the `failure_cause` counts above. `timeout` and `error` point to the provider; `capped` points to our cap.
2. **If it's the cap:** raise `AGENTPAY_INTENT_CHECK_DAILY_CAP` or `AGENTPAY_DISPUTE_NARRATIVE_DAILY_CAP` in `.env` only if the spend is expected, then restart with `$C up -d`. The count resets each UTC day.
3. **If the provider is down for a while:** you can stop calling it with `AGENTPAY_INTENT_CHECK_ENABLED=false` or `AGENTPAY_DISPUTE_NARRATIVE_ENABLED=false`, then `$C up -d`.
   - **What it gives:** no wasted 2.5 s timeouts, and an `unknown` verdict with cause `disabled`.
   - **What it doesn't change:** the Outcomes are the same step_ups. It doesn't make anything more permissive.
4. **Dispute narratives written by code are fine to sign off.** To get a model-written one once the API is back, ask for the next pack version with `POST /v1/disputes/{id}/regenerate` (or Regenerate in the dispute viewer). That needs a new Sign-off.
5. **Don't** lower the step-up threshold or turn the `intent` rule off in a Policy pack to get through an outage. Packs are immutable, and that would make the policy more permissive for everyone.

## When it's over

- New Intent checks come back `match` or `mismatch` again: the query above shows no new `timeout` or `error` causes.
- New packs say `narrative_source: model`.
- If you turned the features off, set the switches back to `true` and run `$C up -d`.
