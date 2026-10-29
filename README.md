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
production swaps an adapter instead of rewriting callers. The rules are in [CLAUDE.md](CLAUDE.md),
and the ones that can be checked mechanically are enforced by
[`tests/architecture`](tests/architecture).

## Run it locally

Prerequisites: [uv](https://docs.astral.sh/uv/) and Docker Desktop.

```bash
uv sync                                        # Python 3.12 + dependencies
docker compose -f infra/docker-compose.yml up -d  # Postgres, Temporal, OTel collector
cp .env.example .env
uv run uvicorn api.main:app --reload           # http://localhost:8000/healthz
```

## Checks (what CI runs on every PR)

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
uv run python -m evals.run_evals
```

## Layout

| Path | Holds |
| --- | --- |
| `api/` | FastAPI app and `/v1` routers |
| `core/` | Domain models, policy engine, signals, ports, settings. No vendor SDKs. |
| `adapters/` | Implementations of the ports: rails, model gateway, Postgres, telemetry |
| `workflows/` | Temporal workflows: step-up consent, dispute pack |
| `agents/` | The dispute evidence agent: tools and versioned prompts |
| `policy_packs/` | Released policy packs and their hash manifest |
| `evals/` | Golden cases and the CI eval gate |
| `migrations/` | Alembic migrations |
| `infra/` | Docker Compose for local dev; deploy config later |
| `web/` | Next.js front end (from Phase 3) |

## License

MIT
