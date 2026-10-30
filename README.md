# AgentPay Guard

A trust layer for payments that AI agents make on a user's behalf. For each agent-initiated
payment it decides, in real time, whether to **allow** it, ask the user for fresh consent
(**step_up**), or **block** it, and it builds the evidence pack when a payment is disputed.

> Status: Phase 2. On top of `v0.1.0` (Mandates, the deterministic policy engine and the audit
> log), Policy pack `default@1.1.0` adds four Signals: Velocity, New merchant, the Intent
> check and Price drift. See [docs/BUILD-PLAN.md](docs/BUILD-PLAN.md) for the plan and GitHub
> Issues for PRDs and tickets.

## Design in one paragraph

The decision path is deterministic: a policy engine reads a versioned, content-hashed policy
pack and Signals worked out before it runs, so no LLM sits on the hot path. Anything that waits (user consent)
or uses an LLM (intent check, dispute narrative) runs on a durable async path. Every external
dependency sits behind a port in [`core/ports.py`](core/ports.py), so moving from MVP to
production swaps an adapter instead of rewriting callers. The rules are in [CLAUDE.md](CLAUDE.md).

Enforced by code from the first commit:

- no vendor SDK imports in `core/` (architecture test);
- no environment reads outside `core/settings.py` (architecture test);
- no `ModelGateway` reference in `core/policy` (architecture test);
- money as integer minor units + ISO currency (the `Money` type rejects floats);
- idempotency keys and tenant IDs in every write-shaped port signature (`mypy --strict`);
- a pure policy engine that imports no clock, store, gateway or I/O (architecture test);
- append-only Audit records (Postgres triggers), with every Decision in the test suite replayed byte for byte;
- latency budgets (`decide()` p99 < 5 ms, `POST /v1/decisions` p95 < 150 ms) and a zero-mismatch eval gate in CI.

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

Every Decision emits a `decision` trace span (Tenant, Outcome, primary Reason code, Policy pack version) over OTLP to the collector, which prints it (`docker compose -f infra/docker-compose.yml logs otel-collector`). Spans carry identifiers and codes only, never merchant names, line items or Agent reasoning. Set `AGENTPAY_TELEMETRY_EXPORTER=none` to build spans without exporting them.

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
| `POST /v1/mandates`, `GET /v1/mandates/{id}` | Record a Payer's Mandate (Agents, caps, MCCs, validity of at most a year, Consent evidence) and read it back with its current status |
| `GET /v1/mandates?payer_id=…` | List a Payer's Mandates, newest first. Pass `next_cursor` back as `cursor` |
| `POST /v1/mandates/{id}/revoke` | End a Mandate for good, recording `revoked_by` (`payer` or `tenant`) |

| `POST /v1/intents`, `GET /v1/intents/{id}` | Before checkout, submit the Payer instruction and the proposed cart for an Intent check by Claude Haiku 5.5. The POST returns only `intent_id` and `expires_at`; the verdict (`match`, `mismatch`, `unknown`) is readable by the Tenant through GET. A timeout, error or unclear reply is stored as `unknown` |
| `POST /v1/decisions`, `GET /v1/decisions/{id}` | Ask whether an Agent may pay under a Mandate. Every Outcome (`allow`, `step_up`, `block`) is a 200 with Reason codes, Risk tier, the Policy pack hash and an Audit record ID. Pass the `intent_id` from `POST /v1/intents` to cite an Intent check; packs from `1.1.0` step up a medium or high payment without a usable match |

How a Decision is reached:

- **Every rule runs** and the most severe result wins: block, then step_up, then allow.
- **Blocks:** a revoked or out-of-date Mandate, an Agent that isn't on the Mandate or is disabled, a currency mismatch, a high-risk MCC (gambling, quasi-cash, money transfer) or one the Mandate doesn't list, and an amount over the per-transaction cap or past the monthly cap.
- **Steps up:** INR 5,000 / USD 100 or more, or reaching 80% of the monthly cap.
- **Spend:** only allowed Decisions count towards the monthly cap, per UTC calendar month.
- **On step_up,** the response carries a pending Consent request (`step_up.consent_request_id`, `expires_at`, `status_url`). Its `consent.status` reads `expired` once the active Policy pack's consent TTL has passed (`consent_ttl_seconds`; 10 minutes for packs that don't set it). Approving it arrives in a later phase.
- **No amounts are disclosed:** responses never include thresholds or caps.

Mandates can't be edited. To change terms, create a new Mandate with `supersedes` set to the old one, and revoke the old one.

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
uv run python -m evals.run_evals
uv run pytest -m perf -s -o addopts=""   # latency budgets
```

The tests need local Postgres running (the `docker compose` step above) and a `.env`. They create and migrate their own `<database>_test` database on each run, so your dev data is left alone.

The Intent check has its own eval set: 30 labelled Payer-instruction and cart pairs in `evals/intent/cases.jsonl`, 6 of them prompt-injection attempts. `uv run python -m evals.run_intent_evals` runs them against the real model with the production prompt and settings. It reports false-match and false-mismatch rates and costs about $0.002 a run. Run it by hand and before each release; it exits 1 if any injection attempt gets a match. The first run of `intent_check@1` on Claude Haiku 5.5 scored 30/30, with no false matches.

Contract tests call real vendor APIs and are never run by CI. Run them by hand with `uv run pytest -m contract -o addopts=""`. They skip themselves when the vendor key (e.g. `AGENTPAY_ANTHROPIC_API_KEY`) isn't set, and the Claude ones cost a fraction of a cent.

The eval gate runs every hand-labelled golden file in `evals/golden/` through the engine, each against the Policy pack its base file names:

- `phase1.jsonl`: 44 cases on `default@1.0.0`, covering every Phase 1 rule and threshold edge.
- `phase2.jsonl`: 36 cases on `default@1.1.0`, covering every Signal Reason code and edge, every way an Intent check can be missing or fail, and the Phase 1 thresholds under the new pack.

Each case changes only a few fields of its base file. The gate prints false-allow and false-block counts per file, and fails on any mismatch, including a wrong Reason code. The full 200+ case set and per-release thresholds come with the Evals PRD.

## Layout

| Path | Holds |
| --- | --- |
| `api/` | FastAPI app and `/v1` routers |
| `core/` | Domain models, policy engine, signals, ports, settings. No vendor SDKs. |
| `adapters/` | Implementations of the ports: rails, model gateway, Postgres, telemetry |
| `workflows/` | Temporal workflows: step-up consent, dispute pack |
| `agents/` | The dispute evidence agent: tools and versioned prompts |
| `policy_packs/` | Released policy packs and their hash manifest |
| `evals/` | Labelled golden cases and the eval gate; the live Intent check eval set |
| `migrations/` | Alembic migrations |
| `infra/` | Docker Compose for local dev; deploy config later |
| `web/` | Next.js front end; pages arrive from Phase 3 |

## Signals (Policy pack `default@1.1.0`)

Signals are facts about a payment beyond the Mandate's terms. Each one can at most send a payment to step_up, so the Payer decides: a Signal never blocks and never turns a step_up into an allow (ADR-0006). A missing or failed Signal steps up too.

| Signal | Steps up when | Reason code |
| --- | --- | --- |
| Velocity | 5 or more earlier Decisions on the Mandate, of any Outcome, in the last 10 minutes | `VELOCITY_HIGH` |
| New merchant | at medium tier or above, the Payer has never had an allowed payment to this merchant under any Mandate | `MERCHANT_NEW` |
| Intent check | at medium tier or above, the request cites no usable Intent check, or its verdict is mismatch or unknown | `INTENT_MISSING`, `INTENT_MISMATCH`, `INTENT_UNKNOWN` |
| Price drift | the amount is more than 2% above the cited Intent check's Quoted total | `PRICE_DRIFT` |

**The Intent check.** Before checkout, the Agent sends the Payer's own instruction and its cart to `POST /v1/intents`. Claude Haiku 5.5 compares them, at low effort with a 2,500 ms timeout. The cart is passed as untrusted data, and Agent reasoning is never sent. The Agent gets back only an `intent_id` and then cites it in `POST /v1/decisions`. A check counts only for the same Mandate, Agent, merchant and Declared MCC, for 30 minutes. A timeout, error or unclear reply is stored as `unknown`. The Payer instruction stays out of the Audit chain; only its hash goes in (ADR-0007).

Settings: `AGENTPAY_INTENT_CHECK_ENABLED` (default `true`), `AGENTPAY_INTENT_CHECK_MODEL` (default `claude-haiku-5-5`) and `AGENTPAY_INTENT_CHECK_TIMEOUT_MS` (default `2500`). While the check is enabled, any environment except `test` needs `AGENTPAY_ANTHROPIC_API_KEY` to start.

## Who is liable (draft)

- **Inside the Mandate:** a payment the Mandate permits, under Consent evidence the Tenant attested, is the Tenant's to resolve with its Payer. If that attestation was false, the Tenant bears the loss (ADR-0005).
- **Outside the Mandate:** if AgentPay Guard allowed a request that a correct evaluation of the recorded inputs would have blocked, that is a false-allow and our defect. Replay of the Audit record shows which case it was.
- **Disabled Agents and revoked Mandates:** protection starts from the moment the change commits. A Decision made before then stands.
- **Signals and the Intent check** add protection but don't move liability. The Tenant attests the Payer instruction it sends, as it attests Consent evidence. If the model wrongly says match, the Decision still applies every Mandate rule, so a wrong match is not by itself a false-allow by AgentPay Guard.

## Known limitations

- **An allow uses up cap even if the Agent never charges.** There is no void yet; it comes with the payment rail.
- **The Declared MCC is trusted.** The merchant category the Agent states isn't yet checked against what the rail reports.
- **Monthly caps reset at midnight UTC,** which is 05:30 IST on the 1st.
- **Decisions within one Tenant queue briefly on that Tenant's Audit chain lock,** so per-Tenant throughput has a ceiling. Measured with every Signal on (`default@1.1.0`, medium payments citing an Intent check): p50 53 ms and p95 62 ms for one client on local Postgres. A load test against a real server is still to do.
- **step_up only creates a pending Consent request.** Approval, notification and the durable workflow come in Phase 3.
- **Only INR and USD,** and one Policy pack for every Tenant.
- **An Agent that holds its Tenant's API key can read Intent verdicts** through `GET /v1/intents/{id}`, and so could keep rewording a cart until it gets a match. The POST never returns the verdict, but the Tenant is accountable for keeping its key from its own Agents.
- **New merchant has a harmless race.** Two parallel first payments to the same merchant under different Mandates can both see it as new. Both just step up.
- **One Intent check can be cited for 30 minutes,** by any Decision on the same Mandate, Agent and merchant.
- **Each Intent check holds a database connection while the model answers,** up to 2.5 s, so that a retry never calls the model twice.

## Proving a Decision

Every Decision writes an Audit record into its Tenant's hash chain. Two commands check the evidence:

```bash
uv run python -m api.admin replay <audit_record_id> --tenant <tenant_id>
uv run python -m api.admin verify --tenant <tenant_id>
```

- **`replay`** re-runs the engine on the recorded inputs with the recorded Policy pack and compares bytes. Exit codes: `0` identical, `1` different (prints a diff), `2` the recorded pack is missing or its hash doesn't match, `3` no such record for that Tenant.
- **`verify`** walks the chain and prints the first broken `seq` if any record was edited, removed or added. Exit codes: `0` clean, `1` broken.

The test suite replays every Decision it makes and verifies every chain it touches.

## Policy packs

The decision rules read their parameters from a Policy pack, `policy_packs/<id>/<version>.yaml`. `policy_packs/manifest.json` records each released version's hash and which version is active. The API refuses to start if the active pack doesn't match its hash, and CI fails any PR that edits or deletes a released pack file.

To change a value, release a new version:

1. Copy the current file to the next version, for example `default/1.1.1.yaml`. Then edit `version` and the values.
2. Run `uv run python -m core.policy.packs --hash policy_packs/default/1.1.1.yaml`, then add the printed hash under `versions` in the manifest. Point `active` at the new version.
3. Keep the old version. Replay needs every released pack.

`policy_packs/schema.json` documents the format. It is generated from the pack model: regenerate it with `uv run python -m core.policy.packs --write-schema`, and a test fails if it's stale.

## License

MIT
