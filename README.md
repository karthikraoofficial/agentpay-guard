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
uv run python -m workflows.worker                 # in a second terminal: the consent worker
cd web && npm ci && npm run dev                   # in a third: the consent screen on :3000
```

Then open http://localhost:8000/healthz (liveness) and http://localhost:8000/docs (OpenAPI).
The Temporal UI is at http://localhost:8233, where each step_up shows up as a `step_up_consent` workflow named after its Consent request ID.

The consent worker hosts three things: the `step_up_consent` workflow, its two activities (notify and expire), and the outbox relay that starts a workflow for every committed step_up. The API never calls Temporal on the hot path. It only nudges a waiting workflow after an approval or decline is stored. Both processes are safe to kill: Temporal resumes every waiting workflow on the next worker, and the relay keeps undelivered events until a start succeeds.

Every Decision emits a `decision` trace span (Tenant, Outcome, primary Reason code, Policy pack version) over OTLP to the collector, which prints it (`docker compose -f infra/docker-compose.yml logs otel-collector`). Spans carry identifiers and codes only, never merchant names, line items or Agent reasoning. Set `AGENTPAY_TELEMETRY_EXPORTER=none` to build spans without exporting them.

## Using the API

Create a Tenant. It gets two API keys, each printed once; only their hashes are stored:

```bash
uv run alembic upgrade head
uv run python -m api.admin create-tenant "Acme Wallet"
uv run python -m api.admin create-key --tenant <id> --scopes decide   # another key, if you need one
```

Each key has scopes (ADR-0011), so an Agent can never approve its own step_up or grant itself more power:

- **The agent key (`decide`)** is the only key to give an Agent. It asks for Decisions and Intent checks.
- **The tenant key (`consent`, `manage`)** stays in your back end. `manage` registers Payers and Agents, grants Mandates, enables and disables Agents, and reads Intent verdicts. `consent` lists Consent requests and, later, resolves them.
- **Any key** can read Decisions, Mandates, Payers, Agents and Consent requests, and can revoke a Mandate.

A key without the scope an endpoint needs gets `403 INSUFFICIENT_SCOPE`. Keys made before scopes existed have all three.

Send a key as `Authorization: Bearer apg_test_…` on every call, and an `Idempotency-Key` header on every write. Available so far:

| Endpoint | Scope | Does |
| --- | --- | --- |
| `POST /v1/payers`, `GET /v1/payers/{id}` | manage / any | Register and fetch a Payer. `contact_ref` must be a token you can resolve, not a raw email or phone number. |
| `POST /v1/agents`, `GET /v1/agents/{id}` | manage / any | Register and fetch an Agent |
| `POST /v1/agents/{id}/disable`, `/enable` | manage | Stop an Agent under every Mandate, or let it act again |
| `POST /v1/mandates`, `GET /v1/mandates/{id}` | manage / any | Record a Payer's Mandate (Agents, caps, MCCs, validity of at most a year, Consent evidence) and read it back with its current status |
| `GET /v1/mandates?payer_id=…` | any | List a Payer's Mandates, newest first. Pass `next_cursor` back as `cursor` |
| `POST /v1/mandates/{id}/revoke` | any | End a Mandate for good, recording `revoked_by` (`payer` or `tenant`) |
| `POST /v1/intents`, `GET /v1/intents/{id}` | decide / manage | Before checkout, submit the Payer instruction and the proposed cart for an Intent check by Claude Haiku 5.5. The POST returns only `intent_id` and `expires_at`; the verdict (`match`, `mismatch`, `unknown`) is readable by the Tenant through GET with a `manage` key. A timeout, error or unclear reply is stored as `unknown` |
| `POST /v1/decisions`, `GET /v1/decisions/{id}` | decide / any | Ask whether an Agent may pay under a Mandate. Every Outcome (`allow`, `step_up`, `block`) is a 200 with Reason codes, Risk tier, the Policy pack hash and an Audit record ID. Pass the `intent_id` from `POST /v1/intents` to cite an Intent check; packs from `1.1.0` step up a medium or high payment without a usable match |
| `GET /v1/consent-requests?status=pending`, `GET /v1/consent-requests/{id}` | consent / any | List Consent requests newest first (filter by `status`: `pending`, `approved`, `declined`, `expired` or `blocked`; pass `next_cursor` back as `cursor`), or read one |
| `POST /v1/consent-requests/{id}/approve` | consent | Record that the Payer said yes, with a required `attestation`. Returns `approved`, or `blocked` with `reason_codes` after the re-check. Same `409`s as decline |
| `POST /v1/consent-requests/{id}/link` | consent | Mint a Consent link to deliver to the Payer: `{url, expires_at}`, where `url` is `<web origin>/consent#<token>`. Minting again revokes the previous link. Only the token's hash is stored, so an idempotent replay returns `url: null` |
| `GET /v1/consent-link`, `POST /v1/consent-link/approve`, `/decline` | none (the link) | The hosted consent screen's API. Send the token in a `Consent-Link` header, never in a path, so it never reaches an access log. The view holds the amount, merchant, Agent, a plain-language line per Reason code, the Intent verdict, the expiry and a stale-Mandate warning, and never Agent reasoning. Approve and decline take an `Idempotency-Key`, go through the same re-check, and record `resolved_via: consent_link`. Any link that is unknown, replaced, used or expired gets `404 CONSENT_LINK_INVALID`. CORS allows only `AGENTPAY_WEB_ORIGIN`, and only here |
| `POST /v1/consent-requests/{id}/decline` | consent | Record that the Payer said no, optionally with an `attestation` (`method`, `reference`, `captured_at`) of how you confirmed it. Writes a Consent resolution into the Audit chain. `409 CONSENT_EXPIRED` at or after `expires_at`, `409 CONSENT_ALREADY_RESOLVED` once resolved |

How a Decision is reached:

- **Every rule runs** and the most severe result wins: block, then step_up, then allow.
- **Blocks:** a revoked or out-of-date Mandate, an Agent that isn't on the Mandate or is disabled, a currency mismatch, a high-risk MCC (gambling, quasi-cash, money transfer) or one the Mandate doesn't list, and an amount over the per-transaction cap or past the monthly cap.
- **Steps up:** INR 5,000 / USD 100 or more, or reaching 80% of the monthly cap.
- **Spend:** allowed Decisions and approved step-ups count towards the monthly cap, per UTC calendar month. An approved step-up counts in the month it was approved.
- **On step_up,** the response carries a pending Consent request (`step_up.consent_request_id`, `expires_at`, `status_url`). Its `consent.status` reads `expired` once the active Policy pack's consent TTL has passed (`consent_ttl_seconds`; 10 minutes for packs that don't set it). Once the Tenant resolves it, `consent.status` reads `approved`, `declined` or `blocked` with `resolved_at`, and `consent.mandate_inactive` warns, while it is pending, that an approval would now be blocked; the Decision's own `outcome` stays `step_up`. The Agent may pay only on `approved`.
- **The consent workflow starts from an outbox** (ADR-0009). A step_up writes a `consent_requested` event in the same transaction as the Decision, so the hot path never calls Temporal. A relay starts one workflow per event, with the Consent request ID as its workflow ID, and keeps the event until the start succeeds. Approving or declining signals the workflow after the answer is stored; if that signal fails, the answer still stands.
- **Approval re-checks the Mandate** (ADR-0010). The engine runs again on the request as it was asked and the Signals recorded then, against the Mandate, Agent and Spent this month as they are at approval. A Payer can lift a step_up but never a block: if the Mandate was revoked or expired, the Agent disabled, or the monthly cap used up while the request waited, the approval is recorded as `blocked` with `consent.reason_codes`.
- **No amounts are disclosed:** responses never include thresholds or caps.

Mandates can't be edited. To change terms, create a new Mandate with `supersedes` set to the old one, and revoke the old one.

Repeating a write with the same `Idempotency-Key` is safe for 24 hours:

- **Same request:** you get the first response back byte for byte, with `Idempotent-Replayed: true`.
- **Different body or endpoint:** `409 IDEMPOTENCY_KEY_REUSED`.
- **First call still running:** `409 IDEMPOTENCY_IN_PROGRESS` with `Retry-After: 1`.
- **First call failed:** errors are never stored, so a retry runs fresh.

Keys are per Tenant.

Errors are `application/problem+json` with a stable `code`, such as `UNAUTHENTICATED`, `INSUFFICIENT_SCOPE`, `IDEMPOTENCY_KEY_MISSING` or `VALIDATION_FAILED`. An ID that belongs to another Tenant returns 404.

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

The consent workflow tests run on Temporal's own servers, which `temporalio` downloads on first use, so the default `pytest` run skips them and CI runs them as a separate step: `uv run pytest -m workflow -o addopts=""`. The exit-criterion test kills a real `python -m workflows.worker` process while the workflow waits. A fresh worker then finishes the workflow from Temporal's history, once by an approval signal and once by the expiry timer.

Contract tests call real vendor APIs and are never run by CI. Run them by hand with `uv run pytest -m contract -o addopts=""`. They skip themselves when the vendor key (e.g. `AGENTPAY_ANTHROPIC_API_KEY`) isn't set, and the Claude ones cost a fraction of a cent.

The eval gate runs every hand-labelled golden file in `evals/golden/` through the engine, each against the Policy pack its base file names:

- `phase1.jsonl`: 44 cases on `default@1.0.0`, covering every Phase 1 rule and threshold edge.
- `phase2.jsonl`: 36 cases on `default@1.1.0`, covering every Signal Reason code and edge, every way an Intent check can be missing or fail, and the Phase 1 thresholds under the new pack.
- `approval.jsonl`: 6 cases on `default@1.1.0` for the re-check at approval, labelled with the Consent resolution: unchanged and cap-near (approved), and Mandate revoked, Mandate expired, Agent disabled and cap used up while waiting (blocked). An approval that goes through where the label says blocked is a false-allow.

Each case changes only a few fields of its base file. The gate prints false-allow and false-block counts per file, and fails on any mismatch, including a wrong Reason code. The full 200+ case set and per-release thresholds come with the Evals PRD.

## Layout

| Path | Holds |
| --- | --- |
| `api/` | FastAPI app and `/v1` routers |
| `core/` | Domain models, policy engine, signals, ports, settings. No vendor SDKs. |
| `adapters/` | Implementations of the ports: rails, model gateway, Postgres, telemetry |
| `workflows/` | Temporal: the `step_up_consent` workflow, its activities, the Workflow port adapter and the worker (the only code that imports `temporalio`) |
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
- **Notify is a log line.** The workflow's notify activity writes one structured `consent_request.created` line (identifiers and times only); signed Tenant webhooks come with Phase 4. Tenants can also list `GET /v1/consent-requests?status=pending`.
- **The expiry record lands after the timer fires,** which can be later than `expires_at` if no worker is running. Status reads `expired` from `expires_at` regardless, and a late approve or decline is refused.
- **An approved step-up doesn't make its merchant known.** New merchant counts only allowed Decisions, so the next payment to that merchant can step up again.
- **Only INR and USD,** and one Policy pack for every Tenant.
- **Scopes only help if the Agent gets only the agent key.** A Tenant that hands an Agent its tenant key, or a key with every scope, gives it back the power to grant Mandates and read Intent verdicts.
- **New merchant has a harmless race.** Two parallel first payments to the same merchant under different Mandates can both see it as new. Both just step up.
- **One Intent check can be cited for 30 minutes,** by any Decision on the same Mandate, Agent and merchant.
- **Each Intent check holds a database connection while the model answers,** up to 2.5 s, so that a retry never calls the model twice.

## Proving a Decision

Every Decision, and every Consent resolution, writes an Audit record into its Tenant's hash chain. Two commands check the evidence:

```bash
uv run python -m api.admin replay <audit_record_id> --tenant <tenant_id>
uv run python -m api.admin verify --tenant <tenant_id>
```

- **`replay`** re-runs the engine on the recorded inputs with the recorded Policy pack and compares bytes. That covers Decisions and approved or blocked Consent resolutions. A declined or expired resolution has no engine run, so it reports nothing to re-run and exits `0`. Exit codes: `0` identical, `1` different (prints a diff), `2` the recorded pack is missing or its hash doesn't match, `3` no such record for that Tenant.
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
