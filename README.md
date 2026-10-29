# AgentPay Guard

A trust layer for payments that AI agents make on a user's behalf. For each agent-initiated
payment it decides, in real time, whether to **allow** it, ask the user for fresh consent
(**step_up**), or **block** it, and it builds the evidence pack when a payment is disputed.

> Status: Phase 0 (scaffold). See [docs/BUILD-PLAN.md](docs/BUILD-PLAN.md) for the plan and
> GitHub Issues for PRDs and tickets.

## Design in one paragraph

The decision path is deterministic: a policy engine reads a versioned, content-hashed policy
pack and cached signals, so no LLM sits on the hot path. Anything that waits (user consent)
or uses an LLM (intent check, dispute narrative) runs on a durable async path. Every external
dependency sits behind a port in [`core/ports.py`](core/ports.py), so moving from MVP to
production swaps an adapter instead of rewriting callers. The rules are in [CLAUDE.md](CLAUDE.md).

Enforced by code from the first commit:

- no vendor SDK imports in `core/` (architecture test);
- no environment reads outside `core/settings.py` (architecture test);
- no `ModelGateway` reference in `core/policy` (architecture test);
- money as integer minor units + ISO currency (the `Money` type rejects floats);
- idempotency keys and tenant IDs in every write-shaped port signature (`mypy --strict`).

## Run it locally

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker Desktop, and Node 20+ for `web/`.

```bash
uv sync                                           # Python 3.12 + locked dependencies
cp .env.example .env                              # the app refuses to start without required values
docker compose -f infra/docker-compose.yml up -d  # Postgres 16, Temporal dev server, OTel collector
uv run uvicorn api.main:asgi_app --factory --reload
```

Then open http://localhost:8000/healthz (liveness) and http://localhost:8000/docs (OpenAPI).
The Temporal UI is at http://localhost:8233.

## Using the API

Create a Tenant and its API key. The key is printed once and only its hash is stored:

```bash
uv run alembic upgrade head
uv run python -m api.admin create-tenant "Acme Wallet"
```

Send the key as `Authorization: Bearer apg_test_…` on every call, and an `Idempotency-Key` header on every write. Available so far:

| Endpoint | Does |
| --- | --- |
| `POST /v1/payers`, `GET /v1/payers/{id}` | Register and fetch a Payer. `contact_ref` must be a token you can resolve, not a raw email or phone number. |
| `POST /v1/agents`, `GET /v1/agents/{id}` | Register and fetch an Agent |
| `POST /v1/agents/{id}/disable`, `/enable` | Stop an Agent under every Mandate, or let it act again |

Repeating a write with the same `Idempotency-Key` is safe for 24 hours:

- **Same request:** you get the first response back byte for byte, with `Idempotent-Replayed: true`.
- **Different body or endpoint:** `409 IDEMPOTENCY_KEY_REUSED`.
- **First call still running:** `409 IDEMPOTENCY_IN_PROGRESS` with `Retry-After: 1`.
- **First call failed:** errors are never stored, so a retry runs fresh.

Keys are per Tenant.

Errors are `application/problem+json` with a stable `code`, such as `UNAUTHENTICATED`, `IDEMPOTENCY_KEY_MISSING` or `VALIDATION_FAILED`. An ID that belongs to another Tenant returns 404.

## Checks (what CI runs on every PR)

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run alembic upgrade head
uv run pytest
```

The tests need local Postgres running (the `docker compose` step above) and a `.env`. They create and migrate their own `<database>_test` database on each run, so your dev data is left alone.

The evals gate joins CI with the Evals PRD.

## Layout

| Path | Holds |
| --- | --- |
| `api/` | FastAPI app and `/v1` routers |
| `core/` | Domain models, policy engine, signals, ports, settings. No vendor SDKs. |
| `adapters/` | Implementations of the ports: rails, model gateway, Postgres, telemetry |
| `workflows/` | Temporal workflows: step-up consent, dispute pack |
| `agents/` | The dispute evidence agent: tools and versioned prompts |
| `policy_packs/` | Released policy packs and their hash manifest |
| `evals/` | Golden cases and the eval gate (from a later phase) |
| `migrations/` | Alembic migrations |
| `infra/` | Docker Compose for local dev; deploy config later |
| `web/` | Next.js front end; pages arrive from Phase 3 |

## Policy packs

The decision rules read their parameters from a Policy pack, `policy_packs/<id>/<version>.yaml`. `policy_packs/manifest.json` records each released version's hash and which version is active. The API refuses to start if the active pack doesn't match its hash, and CI fails any PR that edits or deletes a released pack file.

To change a value, release a new version:

1. Copy the current file to the next version, for example `default/1.0.1.yaml`. Then edit `version` and the values.
2. Run `uv run python -m core.policy.packs --hash policy_packs/default/1.0.1.yaml`, then add the printed hash under `versions` in the manifest. Point `active` at the new version.
3. Keep the old version. Replay needs every released pack.

`policy_packs/schema.json` documents the format. It is generated from the pack model: regenerate it with `uv run python -m core.policy.packs --write-schema`, and a test fails if it's stale.

## License

MIT
