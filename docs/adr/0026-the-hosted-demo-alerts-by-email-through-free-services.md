# The hosted demo alerts by email through free services

The runbooks (#223) showed that every "how you'd notice" step was someone looking: reading a log or running a query. Nothing told the operator that something was wrong (#224). ADR-0023 had turned down a dead-man's-switch service for backups, to avoid a third-party account for a demo. This revisits that for alerting as a whole, and amends that point of ADR-0023 (#227).

The hosted demo now alerts by email through two services. Both have free plans that cover it.
- **Uptime, from outside.** An UptimeRobot monitor checks `https://<AGENTPAY_API_HOST>/healthz` every 5 minutes and emails when it fails. That catches the VM reclaimed or down (ADR-0021), and Caddy or the API down. A check on the VM can't report that its VM is gone.
- **Backup heartbeats, a dead-man's switch.** Two Healthchecks.io checks: the nightly dump (period 1 day, grace 2 hours) and the weekly restore check (period 7 days, grace 6 hours). `infra/pg-backup.sh` pings a check's URL when the job succeeds and its `/fail` URL when it fails. A failure emails, and so does a run that never happens: a cron job that stops running writes no log line to look at.

Why these two, and not more:
- **Alert on symptoms.** `/healthz` stopping is what a visitor to the demo sees. It is liveness only, so a rail, model or worker outage doesn't trip it. Those are covered by the metric alerts below.
- **Every alert needs action.** Each email means a person must do something now: bring the VM or the stack back, or fix a backup. A noisier alert would get ignored.
- **A dead-man's switch for scheduled jobs.** A scheduled job can fail by not running, so the alert has to fire on a missing ping, not only on a failure reported by the job.
- **An outside view.** Both services run off the VM, so losing the VM can't silence them.

What the pings carry, and the secrets:
- **Nothing but the request.** A ping has no body and no data from the database.
- **The ping URLs are secrets.** Anyone with one could fake a success. They are `AGENTPAY_BACKUP_HEARTBEAT_URL` and `AGENTPAY_RESTORE_CHECK_HEARTBEAT_URL` in the VM's `.env`, read by the script only, never by the app, and never printed.
- **Unset means no ping,** as before.
- **A ping that fails is only logged.** It doesn't fail a good backup, and the missed ping then alerts on its own.

The provision wizard's alerts stage walks through creating the checks and the monitor (signing up is the operator's), and writes the two URLs into `.env.production` without showing them.

## Metric alerts through Grafana Cloud (#228)

The runbooks' other symptoms happen inside a running stack, so the uptime check can't see them: the rail failing, the model failing, workflows stuck, Decisions slow. They now alert too, through Grafana Cloud's free plan. A third service, but the same pattern: free, off the VM, and emailing.
- **The metrics.** The API and worker export one or more per runbook symptom over OTLP to the collector (`core/ops_metrics.py`). Identifiers and codes only, as for spans. They are gauges where an alert asks "how many now" (unsettled Payments, the breaker, the outbox), counters where it asks "what share" or "any at all" (Intent verdicts, narrative fallbacks, capped model calls), and a histogram for Decision duration. Decision duration is measured in the app, not with the collector's span-metrics connector: one histogram with buckets around the 150 ms budget, and testable through the Telemetry port.
- **The export.** The collector sends metrics to Grafana Cloud's OTLP gateway only when `AGENTPAY_GRAFANA_OTLP_AUTH` is set, as traces go to Langfuse only with its keys. The auth is a header, not a basicauth extension like Langfuse's: the collector replaces lists when it merges config files, so a second overlay listing its own `service.extensions` would drop Langfuse's.
- **The rules.** They are in `infra/grafana/alert-rules.yaml`, each with its threshold in words and the runbook that says what to do. `python -m adapters.grafana.alerts` pushes them through Grafana's provisioning API and replaces the whole group, so the repo is the source and the rules can be made again on a new stack. Rules pushed this way can't be edited in Grafana's UI, which keeps the repo and the stack the same.
- **The thresholds.** Each is set to need action, not to catch a blip:
  - sweep errors on every run for 3 minutes;
  - a Payment or Refund unsettled past the sweep age for 10 minutes;
  - the breaker open for 5 minutes;
  - over 20% of Intent checks unknown for timeout, error or unparseable over 15 minutes, out of at least 5;
  - any narrative fallback in an hour;
  - any capped model call in 15 minutes;
  - a workflow event over a minute old waiting for 5 minutes;
  - a Consent request 5 minutes past expiry with no resolution for 5 minutes;
  - Decisions' p95 over 150 ms for 15 minutes.
- **A quiet worker alerts.** The worker reports the outbox every 30 s, so that rule treats no data as firing: a worker that's down sends nothing, and that's the symptom. Every other rule treats no data as fine, since a feature not used yet has no series.
- **Applied by Grafana.** Each threshold is a Grafana expression over a query that returns every value. Filtered in PromQL, a healthy reading would be empty, which Grafana reads as no data.

The false-allow rate, the other SLO in the build plan's observability item, isn't measured at run time: a false allow is only known after the fact, from a Dispute or an eval. It stays with the evals.

We considered alerting on metrics alone. Grafana Cloud covers the rail, model, consent and latency symptoms, but it can't see a VM that's gone or a cron job that didn't run: a metric that never arrives looks the same as a feature nobody used. So the uptime check and the heartbeats stay, alongside it. Langfuse takes no metrics. We also considered an email from the worker. That needs SMTP credentials on the VM, and it goes quiet in exactly the cases that matter: the VM, the worker or cron down.
