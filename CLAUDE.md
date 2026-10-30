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

- Setup: `uv sync`, then `cp .env.example .env` (set `AGENTPAY_ANTHROPIC_API_KEY`, or `AGENTPAY_INTENT_CHECK_ENABLED=false`; outside `env=test` the app won't start with neither)
- Local infra: `docker compose -f infra/docker-compose.yml up -d`
- Run the API: `uv run uvicorn api.main:asgi_app --factory --reload` (health: `/healthz`, docs: `/docs`)
- Run the consent worker (Temporal workflow, activities and outbox relay; needs local infra): `uv run python -m workflows.worker`
- Lint and format: `uv run ruff check .` and `uv run ruff format --check .`
- Types: `uv run mypy`
- Migrations: `uv run alembic upgrade head`
- Create a Tenant (prints an agent key and a tenant key once): `uv run python -m api.admin create-tenant <name>`
- Issue another API key: `uv run python -m api.admin create-key --tenant <id> --scopes decide,consent,manage` (any subset; ADR-0011)
- Replay a Decision / verify a Tenant's Audit chain: `uv run python -m api.admin replay <audit_record_id> --tenant <id>` and `uv run python -m api.admin verify --tenant <id>`
- Policy pack hash / schema: `uv run python -m core.policy.packs --hash <pack.yaml>` and `--write-schema`
- Evals: `uv run python -m evals.run_evals` (every golden file in `evals/golden/`, each on its pinned pack; fails on any false-allow, false-block or wrong reason code)
- Live Intent eval (real model, ~$0.002 a run, by hand and before each release, not CI): `uv run python -m evals.run_intent_evals` (exit 1 on any injection false-match)
- Latency budgets: `uv run pytest -m perf -s -o addopts=""` (skipped by the default `pytest` run; CI runs them)
- Consent workflow on Temporal: `uv run pytest -m workflow -o addopts=""` (downloads Temporal's test and dev servers on first use; skipped by the default `pytest` run; CI runs it)
- Tests: `uv run pytest` (needs local infra up and `.env`; tests create their own `<database>_test`)
- Contract tests against real vendor APIs: `uv run pytest -m contract -o addopts=""` (not in CI; skip without the vendor key)
- Web: `cd web && npm ci && npm run build`

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
