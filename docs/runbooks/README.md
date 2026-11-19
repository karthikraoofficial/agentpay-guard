# Runbooks

What to do when something AgentPay Guard depends on fails. Each runbook has the same four parts: how you'd notice, what already happens on its own, what to do, and how you know it's over.

- [A rail outage](rail-outage.md): Razorpay (or the rail in use) is down, slow or refusing.
- [A model outage](model-outage.md): the Claude API is down, slow, erroring, or the Model spend cap is reached.
- [Stuck consent workflows](stuck-consent-workflows.md): Payers aren't notified, or Consent requests don't expire on time.

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
