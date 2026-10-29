# AgentPay Guard — Build Plan (MVP to Production)

Oct 29, 2026 · @Karthik Rao

AgentPay Guard is a trust layer that decides, in real time, whether a payment an AI agent makes on a user's behalf should be allowed, sent for fresh consent, or blocked. It also builds the evidence pack when one is disputed. The MVP runs in about 8 weeks at 10–20 hours a week, and every component is built behind an interface that lets the production version replace it without a rewrite.

## Goal and MVP success criteria

The MVP is done when a reviewer at Stripe or Razorpay can watch three journeys run end to end on a live URL and inspect the evals behind them.

| Criterion | Target for MVP |
| --- | --- |
| Demo journeys | 3 run end to end: clean allow, step-up consent, block + dispute pack |
| Decision latency (hot path) | p95 under 150 ms, no LLM call on this path |
| Eval set | 200+ labelled agent transactions, run in CI on every PR |
| Decision quality | False-allow rate and false-block rate published per release |
| Replay | Any past decision re-runs byte-identical from its audit record |
| Real rails | At least one sandbox wired: Razorpay test mode or Stripe test mode |
| Deployed | Public URL (front end) + API, with a 3-minute walkthrough video |

Out of scope for the MVP: real money, live card data, multi-region, and a production on-call setup. These are listed in the go-live checklist at the end.

## The seam principle: build once, swap parts later

The MVP scales smoothly because every external dependency sits behind an interface (a "seam") from day 1. Going to production means swapping the implementation behind a seam, not changing the code that calls it.

| Seam (interface in code) | MVP implementation | Production swap | Study guide module |
| --- | --- | --- | --- |
| `PaymentRail` | `MockRail` + Razorpay or Stripe test mode | Live keys per tenant, more rails (UPI mandates, cards) | 3 — Tools |
| `PolicyEngine` | Python rules reading versioned YAML policy packs | Same engine; packs edited through a maker-checker admin UI | 8 — Guardrails |
| `Workflow` (durable execution) | Temporal dev server in Docker | Temporal Cloud (or self-hosted cluster) | 7 — Durable execution |
| `ModelGateway` | One wrapper around the Claude API with token and cost logging | Same wrapper + fallback chain, prompt caching, per-tenant budgets | 10, 12 — Architecture, cost |
| `Store` (Postgres) | Postgres 16 in Docker | Managed Postgres (Neon, RDS) with read replicas | 10 — Architecture |
| `Cache` / rate limits | In-process dict | Redis | 11 — Scaling |
| `EventBus` | Postgres outbox table polled by a worker | Kafka or a managed queue; same outbox pattern | 7, 11 |
| `Telemetry` | OpenTelemetry to console + Langfuse free tier | OpenTelemetry collector to Grafana/Datadog + Langfuse | 9 — Observability |
| `Secrets` / config | `.env` file, loaded by one settings module | Cloud secret manager; same settings module | 8 |
| Auth | Static API key per tenant, stored hashed | OAuth client credentials + key rotation | 8 |

Three rules keep the seams honest: business code never imports a vendor SDK directly, every write carries an idempotency key, and every table has a `tenant_id` from the first migration.

## Architecture

Two paths, as in Module 10 of the study guide: a fast synchronous path that decides, and a durable async path for anything that waits or uses an LLM.

&#91;embedded content: AgentPay Guard architecture · hot path and async path\]

The policy engine decides from deterministic rules and cached signals, so the decision never waits on a model. The intent check runs earlier, when the agent builds its cart; a missing or failed result maps to step\_up. Every step writes to the audit log that the dispute agent later cites.

## Tech stack

Pick boring, widely hired-for tools so every choice holds up in a design interview and runs unchanged in production.

| Layer | Choice | Why this one |
| --- | --- | --- |
| Decision API | Python 3.12 + FastAPI + Pydantic v2 | Typed contracts, async, OpenAPI docs for free; same stack as your underwriting app |
| Policy packs | YAML files + JSON Schema, content-hashed | Reviewable in a PR; you already built immutable pack versions once |
| Durable workflows | Temporal (Python SDK) | Durable waits for consent and retries are built in; dev server locally, Temporal Cloud later |
| Database | Postgres 16 + SQLAlchemy 2 + Alembic | One store for mandates, decisions, audit log and outbox |
| LLM | Claude via the Anthropic SDK, behind `ModelGateway` | Small model for intent check, larger model for dispute narrative |
| Front end | Next.js (App Router) + Tailwind | Consent screen, ops console, dispute pack viewer; deploys to Vercel |
| Evals | pytest + JSONL golden set + a small scoring script | Runs in CI; no extra vendor needed |
| Telemetry | OpenTelemetry SDK + Langfuse | Traces every decision, LLM call and tool call with cost |
| Local infra | Docker Compose | Postgres, Temporal, OTel collector with one command |
| Hosting (MVP) | API and workers on Render, front end on Vercel | You've deployed this pairing before (Rebound) |
| Quality gates | Ruff, mypy, pytest, GitHub Actions | Every PR is linted, typed, tested and eval-scored |

## Repository structure

One monorepo, `agentpay-guard`, at `C:\VS_Code\AgentPay_Guard`. Domain code has no vendor imports; vendors live only in `adapters/`.

```text
agentpay-guard/
├─ CLAUDE.md                 # governing rules for Claude Code
├─ docs/
│  ├─ PRD.md                 # 2-page product doc with your point of view
│  ├─ specs/                 # SPEC-01 … SPEC-08 (see below)
│  └─ adr/                   # architecture decision records, one per big choice
├─ api/                      # FastAPI app: /v1/mandates, /v1/decisions, /v1/disputes
├─ core/
│  ├─ domain/                # Pydantic models: Mandate, AgentPaymentRequest, Decision
│  ├─ policy/                # deterministic engine + reason codes
│  ├─ signals/               # velocity, new merchant, price drift, intent match
│  └─ ports.py               # the seams: PaymentRail, ModelGateway, Store, EventBus
├─ adapters/                 # mock_rail, razorpay_rail, stripe_rail, claude_gateway, pg_store
├─ workflows/                # Temporal: step_up_consent, dispute_pack
├─ agents/                   # dispute evidence agent: tools + prompts (versioned)
├─ policy_packs/             # default_v1.0.0.yaml + manifest with content hashes
├─ evals/
│  ├─ golden/                # 200+ labelled cases as JSONL
│  └─ run_evals.py           # scores false-allow, false-block, latency
├─ web/                      # Next.js: consent screen, ops console, dispute viewer
├─ migrations/               # Alembic
├─ tests/                    # unit + contract tests per seam
├─ infra/
│  ├─ docker-compose.yml     # postgres, temporal, otel-collector
│  └─ render.yaml            # deploy blueprint
└─ .github/workflows/ci.yml  # lint, type, test, evals gate
```

## CLAUDE.md governing rules

Paste this as the repo's `CLAUDE.md` in Phase 0. It is what keeps Claude Code from taking shortcuts that break the path to production.

```markdown
# AgentPay Guard — rules for Claude Code

## What this is
A trust layer for agent-initiated payments: mandates, real-time decisions
(allow / step_up / block), durable consent, and dispute evidence packs.
Specs live in docs/specs/. Read the relevant SPEC before changing code.

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

## How to work
- One SPEC section per task. Plan first, then code, then tests, then run evals.
- Small PRs on a feature branch: feat/<spec-id>-<short-name>. Conventional commits.
- Definition of done: ruff + mypy clean, tests pass, evals not worse than main, SPEC updated if behaviour changed.
- If a SPEC is ambiguous, stop and ask. Do not invent policy.
- Keep context small: read only the files the task needs.
```

## SPEC files to write before code

Write these in Phase 0, with Claude drafting and you editing. They are the PM artefacts recruiters will read, and the contract Claude Code builds against.

| SPEC | Covers | Key decisions you must make |
| --- | --- | --- |
| SPEC-01 Domain model | Mandate, AgentPaymentRequest, Decision, ReasonCode, AuditRecord | Mandate fields: merchant categories (MCC), per-txn cap, monthly cap, expiry, allowed agents |
| SPEC-02 Decision API | `POST /v1/decisions` request, response, errors, latency budget | Response codes, idempotency behaviour, what the agent sees on step\_up |
| SPEC-03 Policy engine | Rule order, risk tiers, reason codes, pack format and versioning | Tier thresholds (e.g. above which amount a step-up is mandatory) |
| SPEC-04 Signals | Velocity, new merchant, price drift vs quoted, intent match | Each signal's timeout and fail-closed default |
| SPEC-05 Step-up consent | Durable workflow: notify, wait, approve or decline, expire | Consent channel, timeout (e.g. 10 min), what happens on expiry |
| SPEC-06 Dispute evidence pack | Trail assembly, reason-code mapping, LLM narrative, human sign-off | Which card network reason codes and UPI complaint types you support |
| SPEC-07 Evals | Golden set schema, labelling rules, metrics, CI thresholds | Release gate: e.g. false-allow on high-risk cases must be 0 |
| SPEC-08 Observability and audit | Trace spans, cost attribution, audit hash chain, replay | Retention period; what is redacted before logging |

Your liability model (who bears a loss when an agent overspends within or outside its mandate) belongs in the PRD. It is the strongest PM signal in the whole project.

## GitHub setup and PR workflow

Treat the repo like a team's: every change lands through a pull request that CI has passed. It's good PR practice, and it gives reviewers a readable history.

1. Create a public repo `agentpay-guard` with a README, MIT licence and Python `.gitignore`.
2. Protect `main`: require a pull request and a passing `ci` check before merge; no direct pushes.
3. Add repo secrets for CI: `ANTHROPIC_API_KEY` (evals only), and later the sandbox keys for Razorpay or Stripe test mode.
4. CI (`.github/workflows/ci.yml`) runs on every PR: ruff, mypy, pytest, then `evals/run_evals.py`, which fails the build if any metric is worse than the threshold in SPEC-07.
5. Turn on Dependabot for pip and npm, and GitHub secret scanning.
6. Branch per SPEC section: `feat/spec-03-policy-engine`. Merge with squash; the PR description states what changed and the eval delta.
7. Tag each phase as a release (`v0.1.0` after Phase 1, and so on) with short release notes. Recruiters skim releases.

Per task, the loop is: branch → Claude Code builds against the SPEC → run tests and evals locally → push → open PR → CI green → squash-merge → `/clear` in Claude Code before the next task.

## Build plan by phase

At 10–20 hours a week you reach a live demo in 8 weeks, with a tagged release at each milestone, so the project is presentable even if you stop early.

&#91;embedded content: build plan · 7 phases over 8 weeks\]

Phase 1 gets two weeks because the policy engine and audit replay are the core everything else plugs into. If time runs short, cut Phase 4 to the mock rail and keep the evals.

## Claude Code prompts, phase by phase

Run each prompt in Claude Code in VS Code, opened at the repo root. Run `/clear` before every prompt below, so each phase starts with only `CLAUDE.md` and the SPECs it names in context. Within a phase, run `/clear` again after each merged PR.

**Phase 0 — scaffold** (`/clear` first)

```text
Read CLAUDE.md. Scaffold the repo exactly as in the tree I paste below (copy it from the plan):
api/, core/{domain,policy,signals}, core/ports.py, adapters/, workflows/, agents/, policy_packs/,
evals/, web/ (empty Next.js app), migrations/, tests/, infra/docker-compose.yml with Postgres 16,
Temporal dev server and an OpenTelemetry collector. Add core/settings.py (pydantic-settings, .env),
a /healthz endpoint, ruff + mypy + pytest config, and .github/workflows/ci.yml running all three.
Define the seams in core/ports.py as Protocols: PaymentRail, ModelGateway, Store, EventBus, Telemetry.
No business logic yet. Show me the plan before writing files.
```

**Phase 0b — SPEC drafts** (`/clear` first)

```text
Read CLAUDE.md. Draft docs/specs/SPEC-01 to SPEC-08 using the titles and "key decisions" I paste below.
For every decision, propose a default and mark it [DECIDE] so I can confirm or change it.
Indian context first (UPI Reserve Pay and recurring mandates, RBI tokenisation, DPDP Act),
Stripe shared payment tokens and Issuing spend controls second. Keep each SPEC under 2 pages.
```

**Phase 1 — mandates, policy engine, audit** (`/clear` first)

```text
Read CLAUDE.md, SPEC-01, SPEC-02, SPEC-03. Implement: domain models; Alembic migrations with tenant_id
everywhere; POST/GET /v1/mandates; POST /v1/decisions calling a deterministic PolicyEngine that loads
policy_packs/default_v1.0.0.yaml and checks its manifest hash; reason codes; an append-only audit table
with a hash chain; idempotency keys on all writes. Add a replay command that re-runs a decision from
its audit record and asserts identical output. Write unit tests per rule and a latency test (p95 < 150 ms
on 1,000 decisions locally). Plan first.
```

**Phase 2 — signals and model gateway** (`/clear` first)

```text
Read CLAUDE.md, SPEC-04. Implement signals: velocity, new merchant, price drift vs quoted price, and
intent match. Intent match runs at POST /v1/intents, which the agent calls when it builds the cart,
never inside /v1/decisions. It calls ModelGateway (claude_gateway adapter, small model) with an 800 ms
timeout and caches the result by (mandate_id, intent_hash). The decision reads the cache; a missing,
failed or UNKNOWN result maps to step_up. ModelGateway logs model, prompt version, tokens in/out, cost
and latency as OpenTelemetry spans. Tests must run without network using a fake gateway.
```

**Phase 3 — step-up consent workflow** (`/clear` first)

```text
Read CLAUDE.md, SPEC-05. Implement a Temporal workflow step_up_consent: on a step_up decision, create a
consent request, notify (log + web), wait for an approve/decline signal or expire after the SPEC timeout,
then finalise the decision and write the audit record. Activities are idempotent. Add the Next.js consent
screen showing what the agent wants to buy, why it was flagged, and approve/decline buttons. Include a
test that kills the worker mid-wait and proves the workflow resumes.
```

**Phase 4 — real sandbox rail** (`/clear` first)

```text
Read CLAUDE.md, SPEC-02. Implement one real PaymentRail adapter in test mode only: [Razorpay | Stripe].
Allowed decisions proceed to the rail; blocked ones never reach it. Verify inbound webhooks by signature.
Add contract tests that run the same suite against MockRail and the real adapter (the real one only when
sandbox keys are present). Never log keys or full payloads.
```

**Phase 5 — dispute evidence agent** (`/clear` first)

```text
Read CLAUDE.md, SPEC-06. Build the dispute_pack workflow and a single agent with read-only tools:
get_audit_trail, get_mandate, get_consent_record, get_rail_events. Reason-code mapping is deterministic
code; the LLM (larger model, via ModelGateway) only writes the narrative, citing audit record IDs for
every claim. A checker step rejects any claim without a citation. A human must sign off before the pack
is marked ready. Add the dispute viewer page in Next.js.
```

**Phase 6 — evals, deploy, polish** (`/clear` first)

```text
Read CLAUDE.md, SPEC-07, SPEC-08. Generate 200+ golden cases in evals/golden/ covering clean allows,
over-cap, wrong category, intent mismatch, velocity spikes, price drift, expired mandates and prompt-
injected merchant text. Score false-allow, false-block, step-up rate and p95 latency; fail CI on the SPEC-07
thresholds. Add an ops console page with these metrics. Then write infra/render.yaml and Vercel config
for deployment, and a README with architecture, how to run, and the eval results table.
```

## Production go-live checklist

None of these need a rewrite if the seams hold; each is a swap or an addition. Listing them in the README shows you know the gap between demo and production.

- [ ] Temporal: dev server → Temporal Cloud. For the hosted demo, Render's free tier can't run the Temporal server, so use a Temporal Cloud trial or a small paid Render private service.
- [ ] Postgres: Docker → managed Postgres with backups, point-in-time recovery and a read replica for the ops console.
- [ ] Redis for rate limits and the intent-signal cache; outbox worker → managed queue.
- [ ] Horizontal scale: stateless API pods behind a load balancer; workers scaled on Temporal task-queue depth.
- [ ] Model gateway: fallback chain, prompt caching, per-tenant token budgets and a kill switch per tenant and per tool.
- [ ] Security: OAuth client credentials, key rotation, secret manager, signed webhooks, pen test, dependency audit.
- [ ] Compliance: DPDP-aligned retention and erasure, PCI scope kept at zero (tokens only), audit log export for disputes.
- [ ] Policy packs: maker-checker approval UI; release gate blocks a pack whose evals regress.
- [ ] Observability: OTel collector to a hosted backend, SLO alerts on p95 decision latency and false-allow rate.
- [ ] Load test: decision API at your target throughput (state it, e.g. 500 decisions/s) with p95 still under 150 ms.
- [ ] Runbooks: rail outage (fail to step\_up or block, never allow), model outage, stuck consent workflows.
