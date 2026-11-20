# The hosted demo alerts by email through two free services

The runbooks (#223) showed that every "how you'd notice" step was someone looking: reading a log or running a query. Nothing told the operator that something was wrong (#224). ADR-0023 had turned down a dead-man's-switch service for backups, to avoid a third-party account for a demo. This revisits that for alerting as a whole, and amends that point of ADR-0023 (#227).

The hosted demo now alerts by email through two services. Both have free plans that cover it.
- **Uptime, from outside.** An UptimeRobot monitor checks `https://<AGENTPAY_API_HOST>/healthz` every 5 minutes and emails when it fails. That catches the VM reclaimed or down (ADR-0021), and Caddy or the API down. A check on the VM can't report that its VM is gone.
- **Backup heartbeats, a dead-man's switch.** Two Healthchecks.io checks: the nightly dump (period 1 day, grace 2 hours) and the weekly restore check (period 7 days, grace 6 hours). `infra/pg-backup.sh` pings a check's URL when the job succeeds and its `/fail` URL when it fails. A failure emails, and so does a run that never happens: a cron job that stops running writes no log line to look at.

Why these two, and not more:
- **Alert on symptoms.** `/healthz` stopping is what a visitor to the demo sees. It is liveness only, so a rail, model or worker outage doesn't trip it. Those stay found by looking (`docs/runbooks/`), since on the demo each one degrades safely: decisions step up, Payments wait, Consent requests still resolve from Postgres.
- **Every alert needs action.** Each email means a person must do something now: bring the VM or the stack back, or fix a backup. A noisier alert would get ignored.
- **A dead-man's switch for scheduled jobs.** A scheduled job can fail by not running, so the alert has to fire on a missing ping, not only on a failure reported by the job.
- **An outside view.** Both services run off the VM, so losing the VM can't silence them.

What the pings carry, and the secrets:
- **Nothing but the request.** A ping has no body and no data from the database.
- **The ping URLs are secrets.** Anyone with one could fake a success. They are `AGENTPAY_BACKUP_HEARTBEAT_URL` and `AGENTPAY_RESTORE_CHECK_HEARTBEAT_URL` in the VM's `.env`, read by the script only, never by the app, and never printed.
- **Unset means no ping,** as before.
- **A ping that fails is only logged.** It doesn't fail a good backup, and the missed ping then alerts on its own.

The provision wizard's last stage walks through creating the checks and the monitor (signing up is the operator's), and writes the two URLs into `.env.production` without showing them.

We considered alerting from Langfuse or an OTel backend on the metrics the collector already sends. That would cover the rail, model and latency symptoms too, but it means a metrics backend with alert rules, and it still couldn't see a VM that's gone or a cron job that didn't run. We also considered an email from the worker. That needs SMTP credentials on the VM, and it goes quiet in exactly the cases that matter: the VM, the worker or cron down. Wider SLO alerting, on p95 decision latency and the false-allow rate, stays the checklist's observability item.
