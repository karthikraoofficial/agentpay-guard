# Runbooks

What to do when something AgentPay Guard depends on fails. Each runbook has the same four parts: how you'd notice, what already happens on its own, what to do, and how you know it's over.

- [A rail outage](rail-outage.md): Razorpay (or the rail in use) is down, slow or refusing.
- [A model outage](model-outage.md): the Claude API is down, slow, erroring, or the Model spend cap is reached.
- [Stuck consent workflows](stuck-consent-workflows.md): Payers aren't notified, or Consent requests don't expire on time.
- [Slow decisions](slow-decisions.md): `POST /v1/decisions` is slower than its 150 ms p95 budget.

## Alerts

On the hosted demo, alerts email the operator (ADR-0026). Two watch the VM from outside:
- **Uptime (UptimeRobot):** `https://<AGENTPAY_API_HOST>/healthz` failed from outside. The VM was reclaimed or is down, or Caddy or the API is. `/healthz` is liveness only, so a rail, model, worker or Temporal outage doesn't trip it. On the VM, `$C ps` and `$C logs --since 1h caddy api`; if the VM is gone, the README's Deploy section rebuilds it and Backups restores the newest dump.
- **Backup heartbeat (Healthchecks.io):** a nightly dump or weekly restore check failed, or didn't run on time. The reason is in `~/agentpay-backups/backup.log` or `check.log`. If there's no new line at all, cron didn't run it: check `crontab -l` for the two `# agentpay-backup` jobs (`bash infra/pg-backup.sh install-cron` puts them back). Fixed, run the job by hand: its ping clears the alert.

The rest are metric alerts in Grafana Cloud, over metrics the API and worker export through the collector. Each runbook's "How you'd notice" names its own, and `infra/grafana/alert-rules.yaml` holds every rule and threshold. Push a change with `uv run python -m adapters.grafana.alerts`. A metric alert's email links to its runbook.

## Running the commands

The commands are written for the hosted demo (ADR-0021). On the VM, from `~/agentpay-guard`:

```bash
C="docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.prod.yml"
psql() { $C exec -T postgres psql -U agentpay -d agentpay "$@"; }
```

On a local stack, use `C="docker compose --env-file .env -f infra/docker-compose.yml"` with the same `psql`. The admin commands then run with `uv run python -m api.admin …` instead of `$C exec -T api python -m api.admin …`.

Every query here only reads. Each was run against the hosted demo when the runbook was written (#223).

## After any incident

Check that the evidence still holds for each Tenant involved:

```bash
$C exec -T api python -m api.admin verify --tenant <tenant_id>          # the Audit chain
$C exec -T api python -m api.admin replay <audit_record_id> --tenant <id> # one Decision, byte for byte
```
