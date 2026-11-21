# AgentPay Guard — rules for Claude Code

## What this is
A trust layer for agent-initiated payments: mandates, real-time decisions
(allow / step_up / block), durable consent, and dispute evidence packs.
Specs (PRDs) and tickets live as GitHub issues (see Agent skills below); "SPEC" below means the PRD issue.
Read the relevant PRD and ticket before changing code. docs/BUILD-PLAN.md is the overall plan.

## Non-negotiables
1. The decision path is deterministic. No LLM call inside core/policy.
   LLM output may only arrive as a signal with a timeout and a fail-closed default (step_up).
2. Domain code (core/) never imports a vendor SDK. Vendors live in adapters/ behind core/ports.py.
3. Every table has tenant_id. Every query filters by it.
4. Every write endpoint and every rail call takes an idempotency key.
5. Every decision writes an append-only audit record (inputs, pack version + hash, signals, outcome).
   A decision must replay byte-identical from that record.
6. Policy packs are immutable once released. Changes = a new version + manifest hash.
7. Never store card numbers, UPI PINs or raw credentials. Tokens and masked values only.
8. Money is integer minor units (paise / cents) + ISO currency. Never floats.
9. All config through core/settings.py. No os.environ elsewhere. No secrets in code or logs.
10. Every LLM call goes through ModelGateway and logs model, prompt version, tokens in/out, cost, latency.

## Commands

Run from the repo root. These are the same commands CI runs.

- Setup: `uv sync`, then `cp .env.example .env` (set `AGENTPAY_ANTHROPIC_API_KEY`, or `AGENTPAY_INTENT_CHECK_ENABLED=false`; outside `env=test` the app won't start with neither; Tenant webhooks also need `AGENTPAY_WEBHOOK_ENCRYPTION_KEY`, see `.env.example`). The rail is `mock` by default and needs no account; `AGENTPAY_RAIL=razorpay` needs the Razorpay test key id, key secret and webhook secret, and only `rzp_test_` keys are accepted
- Local infra: `docker compose --env-file .env -f infra/docker-compose.yml up -d` (the env file only switches on the collector's Langfuse exporter, when `AGENTPAY_LANGFUSE_PUBLIC_KEY` and `AGENTPAY_LANGFUSE_SECRET_KEY` are set)
- Move an existing stack from Postgres 16 to 18 (once; ADR-0022): `bash infra/pg-upgrade.sh` locally, `bash infra/pg-upgrade.sh --prod` on the VM. It dumps the old `pgdata` volume, restores into `pgdata18`, checks the row counts, and keeps the old volume and the dump
- Hosted demo backups (ADR-0023; on the VM, from the repo root; the wizard's backups stage installs the cron jobs): `bash infra/pg-backup.sh dump` (nightly: dump, keep 14, upload through `AGENTPAY_BACKUP_PAR_URL`), `bash infra/pg-backup.sh check` (weekly: restore the newest dump into a throwaway Postgres and `verify` every Tenant); each pings its Healthchecks.io heartbeat (`AGENTPAY_BACKUP_HEARTBEAT_URL`, `AGENTPAY_RESTORE_CHECK_HEARTBEAT_URL`; ADR-0026), or its `/fail`, `bash infra/pg-backup.sh install-cron`; `bash infra/oci-create-backup-bucket.sh <env-file>` makes the bucket and its write-only upload link (OCI CLI, on your machine)
- Run the API: `uv run uvicorn api.main:asgi_app --factory --reload` (health: `/healthz`, docs: `/docs`)
- Run the consent worker (Temporal consent and Dispute pack workflows, activities, outbox relay, and the Payment debit step and reconciliation sweep, which are off on the mock rail unless `AGENTPAY_MOCK_SETTLE_AFTER_SECONDS` is set (ADR-0019); needs local infra): `uv run python -m workflows.worker`
- Lint and format: `uv run ruff check .` and `uv run ruff format --check .`
- Types: `uv run mypy`
- Migrations: `uv run alembic upgrade head`
- Create a Tenant (prints an agent key and a tenant key once): `uv run python -m api.admin create-tenant <name>`
- Issue another API key: `uv run python -m api.admin create-key --tenant <id> --scopes decide,consent,manage` (any subset; ADR-0011)
- Replay a Decision / verify a Tenant's Audit chain: `uv run python -m api.admin replay <audit_record_id> --tenant <id>` and `uv run python -m api.admin verify --tenant <id>`; check a Dispute's Evidence packs: `uv run python -m api.admin verify-pack <dispute_id> --tenant <id>`
- Policy pack hash / schema: `uv run python -m core.policy.packs --hash <pack.yaml>` and `--write-schema`
- Evals: `uv run python -m evals.run_evals` (every golden file in `evals/golden/`, each on its pinned pack; fails on any false-allow, false-block or wrong reason code, and on any Dispute case with the wrong network codes, Evidence strength or missing evidence, or any model reply the citation checker judges wrongly, and when the committed Eval report or README eval table is stale; regenerate both with `uv run python -m evals.run_evals --write-report`)
- Release gate for a Candidate pack (ADR-0027): `uv run python -m evals.release_gate --candidate default@<version>` decides every Decision, approval and Dispute golden case on the active pack and on the candidate (Dispute cases when both have Dispute rules), and lists each changed answer as declared or undeclared, reading the candidate's `policy_packs/<id>/<version>.declared-changes.yaml` (ADR-0028) (exit 1 on an undeclared change, a declared change that doesn't happen as declared, a high-risk loosening or a lower Evidence strength even if declared, a candidate that drops the active pack's Dispute rules, an undeclared change to a pack window the API reads (Consent TTL, Payment window, Refund window; declared under `windows:`), or a label the candidate misses in its own golden files; 2 on a bad pin or a declarations file that fails its schema). `--active` defaults to the Manifest's active version; `--write-schema` regenerates `policy_packs/declared-changes.schema.json`. CI's released-packs check: `uv run python -m evals.released_packs <base-commit>`. CI's Release gate step, on pull requests that add a pack or declarations file or change the Manifest (each new version, and a moved active version, against the base branch's active one): `uv run python -m evals.gate_ci <base-commit> [--summary <file>]`
- Live Intent eval (real model, ~$0.003 a run, by hand and before each release, not CI): `uv run python -m evals.run_intent_evals` (exit 1 on any injection false-match, 3 if any injection case got no answer from the model)
- Live dispute narrative eval (real model, ~$0.12 a run, by hand and before each release, not CI): `uv run python -m evals.run_dispute_evals` (exit 1 if an accepted narrative follows an injection)
- Latency budgets: `uv run pytest -m perf -s -o addopts=""` (skipped by the default `pytest` run; CI runs them)
- Consent workflow on Temporal: `uv run pytest -m workflow -o addopts=""` (downloads Temporal's test and dev servers on first use; skipped by the default `pytest` run; CI runs it)
- Tests: `uv run pytest` (needs local infra up and `.env`; tests create their own `<database>_test`)
- End to end on the mock rail (part of `pytest`), an allow to a captured Payment that the Tenant then refunds, and that Payment disputed to a signed-off Evidence pack that passes `verify-pack`: `uv run pytest tests/api/test_end_to_end.py`; the real Razorpay half is the runbook in the README
- Contract tests against real vendor APIs: `uv run pytest -m contract -o addopts=""` (not in CI; skip without the vendor key)
- Register a Razorpay test card for those contract tests (prints the two `AGENTPAY_RAZORPAY_TEST_*` refs for `.env`; test tokens expire within about a day, so re-run before each contract run): `uv run python -m adapters.razorpay.wizard`
- Hosted demo (ADR-0021; on the VM, with `.env` copied from `.env.production.example`): `docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.prod.yml up -d --build`
- Push the metric alert rules to Grafana Cloud (ADR-0026; on your machine, with `AGENTPAY_GRAFANA_URL`, `AGENTPAY_GRAFANA_API_TOKEN` and `AGENTPAY_ALERT_EMAIL` in `.env`): `uv run python -m adapters.grafana.alerts` (`--dry-run` prints the rule group; `--test-alert` emails a test notification, since the pushed contact point has no Test button in Grafana's UI; the service account needs the Admin role). The rules and thresholds are in `infra/grafana/alert-rules.yaml`, each naming its runbook; the metrics they read are in `core/ops_metrics.py`. The collector sends metrics to Grafana only with `AGENTPAY_GRAFANA_OTLP_AUTH` set
- Provision the hosted demo end to end (interactive; Oracle VM, Langfuse, Vercel, Demo Tenant, backups, alerts; never echoes secrets): `bash infra/provision-wizard.sh`. On Windows, run it and the `oci-*.sh` scripts from Git Bash: PowerShell's `bash` is WSL's, and they stop with that hint under WSL (`infra/wsl-guard.sh`, #203). Its stage 1 can run `bash infra/oci-create-vm.sh [--guardrails]` (OCI CLI: network + VM, retries on no capacity; `--guardrails` adds the Always Free quota and spend alert)
- Production stack smoke test (builds the image, starts the stack from `.env.production.example` on ports 8080/8443, checks `/healthz` through Caddy, tears it down; CI's `deploy` job): `bash infra/smoke-prod.sh`
- Demo journeys against a deployment on the mock rail (Demo Tenant keys in `.env` as `AGENTPAY_DEMO_AGENT_KEY` and `AGENTPAY_DEMO_TENANT_KEY`; journeys 3 and 4 need mock settle and a worker): `uv run python -m demo --base-url <api> [--journey 1|2|3|4]`
- Web: `cd web && npm ci && npm run lint && npm run typecheck && npm run build` (CI runs the same in its `web` job)
- Module build graph (buildviz, Node 18+): `node tools/buildviz/buildviz.js` serves it at http://localhost:4477; `node tools/buildviz/buildviz.js run [<module-id>] [--downstream]` builds from the terminal (logs in `.buildviz/logs/<id>.log`). Modules and their `dependsOn` edges are in `buildviz.config.json`, which follows the real imports between packages: register a new top-level package there. Its `pytest` node needs local infra up. Don't edit `.buildviz/`

## How to work
- One SPEC section per task. Plan first, then code, then tests, then run evals.
- Small PRs on a feature branch: feat/<spec-id>-<short-name>. Conventional commits.
- Definition of done: ruff + mypy clean, tests pass, evals not worse than main, SPEC updated if behaviour changed.
- If a SPEC is ambiguous, stop and ask. Do not invent policy.
- Keep context small: read only the files the task needs.

## Agent skills

### Issue tracker

Issues and PRDs live in GitHub Issues on karthikraoofficial/agentpay-guard (via `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
