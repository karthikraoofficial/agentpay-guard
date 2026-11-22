# AgentPay Guard

A trust layer that decides whether a payment an AI agent makes on a person's behalf is allowed, sent back for fresh consent, or blocked.

## Parties

**Tenant**:
The business that integrates AgentPay Guard and calls its API, such as an agent platform, wallet or PSP. All other records belong to exactly one Tenant.
_Avoid_: Client, customer, account

**Payer**:
The person whose money moves. A Payer is registered by and belongs to one Tenant, and is the only party who can grant a Mandate or approve a step-up. A Payer or their Tenant can revoke a Mandate.
_Avoid_: User, customer, end user

**Agent**:
A piece of AI software, registered with a Tenant, that asks to make payments for a Payer.
_Avoid_: Bot, assistant

**API key scope**:
What a Tenant's API key may do: decide (Agent traffic), consent (resolving Consent requests) or manage (granting Mandates and registering or switching Agents and Payers). An Agent is only ever given a decide key. Any key can read its own scopes from `GET /v1/api-keys/self`.
_Avoid_: Role, permission

**MCP server**:
The process an Agent's MCP host launches (`python -m guard_mcp`) so the Agent can ask for a Decision through tools instead of HTTP. It calls the API with one key that holds exactly the decide scope, and won't start with any other. No tool can resolve a Consent request or change a Mandate.
_Avoid_: MCP adapter, MCP plugin, connector

**Disabled agent**:
An Agent its Tenant has switched off. Its requests are blocked under every Mandate until it is re-enabled.
_Avoid_: Revoked agent, suspended agent

**Demo Tenant**:
The Tenant the owner creates on the hosted demo with `api.admin create-tenant`, so visitors can run the demo journeys. It is an ordinary Tenant on the mock rail with mock settle on, held to the Model spend cap like any other. Its two keys are named in config (`AGENTPAY_DEMO_AGENT_KEY`, `AGENTPAY_DEMO_TENANT_KEY`), test keys only, and held by the Demo runner and `python -m demo`, never by a browser (ADR-0031). The API itself still treats it like any other Tenant.
_Avoid_: Test tenant, sandbox tenant, public tenant

## Authority

**Mandate**:
A Payer's standing permission for named Agents to spend within stated limits.
_Avoid_: Authorisation, consent, policy

**Mandate status**:
Whether a Mandate can be used at a given moment: active, revoked, expired or not yet valid. Worked out from the time it is read, never stored, except that Revocation is recorded.
_Avoid_: State, lifecycle

**Consent evidence**:
The Tenant's record of how and when the Payer agreed to a Mandate. The Tenant attests to it; AgentPay Guard does not verify it.
_Avoid_: Proof of consent, authorisation

**Revocation**:
The Payer or Tenant permanently ending a Mandate. A revoked Mandate cannot be reactivated.
_Avoid_: Cancellation, suspension, deactivation

**Superseding Mandate**:
A new Mandate that replaces an earlier one for the same Payer with changed terms. The earlier one is revoked before or after the new one is granted.
_Avoid_: Mandate update, amendment

**Per-transaction cap**:
The largest single payment a Mandate permits.
_Avoid_: Limit, max amount

**Monthly cap**:
The most a Mandate permits in total across one calendar month (UTC), counting only allowed payments and approved step-ups, less Voids.
_Avoid_: Budget, rolling limit

**Allowed MCCs**:
The merchant category codes a Mandate permits. Anything not listed is outside the Mandate.
_Avoid_: Categories, whitelist

**Spent this month**:
The sum of allowed payments and approved step-ups under a Mandate in the current calendar month (UTC), less any that were Voided and less processed Refunds, checked against the Monthly cap. An approved step-up counts in the month it was approved, not the month it was requested, and a Refund comes off in the month its Decision counted, not the month it was made.
_Avoid_: Usage, balance

## Decisions

**Agent payment request**:
An Agent's ask to pay a specific merchant a specific amount under a named Mandate.
_Avoid_: Transaction, payment, order

**Decision**:
The answer to one Agent payment request: exactly one Outcome, with the Reason codes and Policy pack version behind it.
_Avoid_: Verdict, result, response

**Outcome**:
One of allow (the Agent may pay), step_up (the Payer must approve first) or block (final refusal).
_Avoid_: Status, action

**Reason code**:
A stable, machine-readable label for why a rule fired. A Decision lists them in order, and the first is the primary reason.
_Avoid_: Error code, rule ID

**Consent resolution**:
How a Consent request ended: approved, declined, expired, or blocked (the Payer approved but the Mandate's terms no longer permit the payment). Recorded as a new Audit record and never by changing the original Decision.
_Avoid_: Consent update, final decision

**Attestation**:
The Tenant's record of how it confirmed that the Payer was the one answering a Consent request. The Tenant attests to it; AgentPay Guard does not verify it.
_Avoid_: Proof, verification, Consent evidence (that is for Mandates)

**Consent link**:
An expiring link to one Consent request, which the Tenant delivers to the Payer. Whoever opens it is treated as the Payer, and it is used up by the first Consent resolution, not by being opened. A newer link for the same request replaces it.
_Avoid_: Approval link, magic link

**Step-up threshold**:
The amount, set per currency in the Policy pack, at or above which a payment needs Payer approval even inside the Mandate's caps.
_Avoid_: Limit, cap

**Risk tier**:
A low, medium or high band for an Agent payment request, set by its amount against Policy pack bands for that currency.
_Avoid_: Risk score, risk level

**High-risk MCC list**:
Merchant category codes the Policy pack blocks for every Tenant and Mandate.
_Avoid_: Blacklist, deny-list

**Rule result**:
What one rule concluded for one Agent payment request: pass, step_up, block, or not evaluated (the rule was off or couldn't apply).
_Avoid_: Check, verdict

**Consent request**:
A pending ask to the Payer to approve or decline one step_up Decision.
_Avoid_: Step-up request, approval, challenge

**Declared MCC**:
The merchant category code the Agent states in an Agent payment request. On an acquiring rail it can never be confirmed, because the rail only knows the Merchant of record.
_Avoid_: MCC (when the source matters)

**Merchant of record**:
The party the payment rail actually pays, as the rail sees it. It is distinct from the merchant named in an Agent payment request, which the rail does not see.
_Avoid_: Merchant (when the rail's view matters), payee

**Agent reasoning**:
The Agent's own free-text explanation for a payment, kept as evidence and never used to decide.
_Avoid_: Justification, intent

**False-allow**:
An allow where the label says the request should have been stepped up or blocked. The most serious kind of eval failure.
_Avoid_: False negative, miss

**False-block**:
A block where the label says the request should have been allowed or stepped up.
_Avoid_: False positive, false decline

## Payments

**Payment**:
The money movement for one allowed Decision or approved step-up, for exactly its amount. A Decision has at most one Payment. A Payment is scheduled from the moment it is made, and becomes captured or failed when the rail reports back, or failed at once if the rail refuses it. On a rail with a Pre-debit notice, the card is debited only once the notice period ends, and a Payment whose Mandate was revoked, or whose Agent was disabled, by then fails instead, as does one whose Pre-debit notice failed.
_Avoid_: Transaction, Agent payment request (that is only the ask)

**Pre-debit notice**:
The rail's message to the Payer before a recurring debit, saying how much will be taken and when. On card recurring payments the debit can only follow 36 hours after it, so a Payment stays scheduled across that time.
_Avoid_: Notification (too general), reminder

**Rail charge**:
The rail's own record of the debit for one Payment: what the rail says happened, which a Payment follows.
_Avoid_: Payment (that is our record), transaction

**Payment window**:
How long an Agent has to start its Payment, counted from the allow or from the approval of a step-up, and set in the Policy pack. After it, the Decision can no longer be paid.
_Avoid_: Expiry, TTL

**Void**:
Releasing an allow or approved step-up from Spent this month, because its Payment failed or the Tenant cancelled it before paying. Recorded as a new Audit record and never by changing the Decision.
_Avoid_: Cancellation, reversal, refund (a refund returns captured money)

**Refund**:
Money returned to the Payer on a captured Payment, in full or in parts, never more in all than was captured. Only the Tenant starts one, through our API (ADR-0024). It is pending until the rail answers, then processed or failed, and each step is a `refund` Audit record. The Payment stays captured.
_Avoid_: Void (that releases a Decision that was never paid), reversal, chargeback (that is the Payer's network dispute)

**Refund window**:
How long after capture a Payment may be refunded, set in the Policy pack (180 days from `default@1.5.0`; earlier packs read as the same).
_Avoid_: Refund period, expiry

**Tenant webhook**:
A signed notice that AgentPay Guard sends to a Tenant's own endpoint when something it cares about happens, such as a Consent request being created or a Payment being captured. Delivered at least once.
_Avoid_: Callback, notification (that is what the Tenant sends the Payer), rail webhook

**Rail webhook**:
A signed message from the rail saying how a Rail charge ended, which settles its Payment as captured or failed, or how a refund ended, which settles its Refund as processed or failed. Each one is acted on at most once.
_Avoid_: Callback, notification, Tenant webhook (that one goes to a Tenant)

**Reconciliation**:
Asking the rail how a Payment or Refund ended when no Rail webhook has said so after a while, and settling it the same way a webhook would. A Payment the rail has no Rail charge for is failed, never charged late; a Refund the rail never had is failed, so no money moved and it can be asked for again.
_Avoid_: Retry, polling

**Mock settle**:
A demo-only setting under which Reconciliation captures every debited mock Payment once it is old enough, so a hosted demo can dispute a captured Payment. It is refused with any real rail (ADR-0019).
_Avoid_: Fake capture, auto-capture

**Rail token**:
The rail's reference to the Payer's payment instrument, bound to a Mandate when it is granted, shown only in masked form. Never the card number or UPI PIN.
_Avoid_: Card, payment method, credentials

## Signals

**Signal**:
A fact about an Agent payment request beyond the Mandate's terms, such as Velocity or a New merchant, worked out before the Decision and recorded with it. A Signal can at most send a request to step_up. It never blocks and never turns a step_up into an allow.
_Avoid_: Risk score, feature, check

**Velocity**:
The number of Decisions, of any Outcome, made on a Mandate within a recent window before the current request.
_Avoid_: Rate, frequency

**New merchant**:
A merchant that the Payer has never had an allowed payment to, under any of their Mandates. A Voided allow doesn't count, because it was never paid.
_Avoid_: Unknown merchant, first-time merchant

**Payer instruction**:
The Payer's own words for what they asked the Agent to buy, as captured and attested by the Tenant. It is distinct from Agent reasoning.
_Avoid_: Intent, prompt, goal

**Intent check**:
A model-assisted comparison of a proposed cart against the Payer instruction, run before the Agent asks for a Decision.
_Avoid_: Intent match, intent classification

**Intent verdict**:
The result of an Intent check: match, mismatch or unknown. A check that fails or times out gives unknown. A verdict can be used for a limited time, by Decisions on the same Mandate and Agent at the same merchant.
_Avoid_: Intent score, intent result

**Model spend cap**:
How many model calls a Tenant may make per UTC day for each purpose (Intent checks, dispute narratives). Past it the model isn't called and the caller fails closed: the Intent verdict is unknown and the narrative is written by code (ADR-0020).
_Avoid_: Budget, quota, rate limit

**Quoted total**:
The cart total submitted with an Intent check: the price the Agent said it would pay.
_Avoid_: Quote, quoted price, expected amount

**Price drift**:
How far an Agent payment request's amount is above the Quoted total of the Intent check it cites. A price below the Quoted total is never drift.
_Avoid_: Price change, variance

## Policy

**Policy pack**:
A versioned, immutable set of rule parameters and rule order that the engine decides with. Identified by id and semver version.
_Avoid_: Policy, ruleset, config

**Pack hash**:
The SHA-256 fingerprint of a Policy pack's content, which proves exactly which parameters a Decision used.
_Avoid_: Checksum, version

**Manifest**:
The record of every released Policy pack version, its Pack hash, and which version is active.
_Avoid_: Registry, index

**Candidate pack**:
The Policy pack version a change proposes to make active, which must pass the Release gate first.
_Avoid_: New pack, draft pack, next pack

**Release gate**:
The check a Candidate pack must pass before it becomes active. It decides every Golden case on both the active pack and the candidate. Dispute cases are compared only when both packs have Dispute rules. It fails on:

- any change that isn't a Declared change: in Outcome, Reason codes, Risk tier or Consent resolution, or for a Dispute case in Evidence strength, network reason codes or missing evidence;
- a Declared change that doesn't happen as declared;
- any loosening of a case that is high on either pack, and any lower Evidence strength, even when declared;
- a candidate that drops the Dispute rules the active pack has;
- a change to a pack window the API reads (Consent TTL, Payment window, Refund window) that isn't declared. A longer Consent TTL or Payment window is flagged as a loosening but passes when declared;
- a candidate that misses a label in its own golden files (ADR-0027).

_Avoid_: Pack check, eval gate, regression test

**Declared change**:
A Golden case whose answer a Candidate pack is meant to change. It is one entry in the candidate's declarations file, `policy_packs/<id>/<version>.declared-changes.yaml`, and gives the golden file, the case id, the answer on the active pack, the answer on the candidate (for a Decision or approval case, the Outcome, Reason codes, Risk tier and any Consent resolution; for a Dispute case, the Evidence strength, the Visa and Mastercard codes and the missing evidence), and a reason. A pack window is declared the same way, in the file's `windows:` list, with its value on each pack. The Release gate fails when a declared change doesn't happen or returns a different answer. The file isn't hashed into the pack, and once released it can't be changed (ADR-0028).
_Avoid_: Expected diff, waiver, exception

## Evidence

**Decision time**:
The single server-read UTC instant a Decision was made at, used for every time comparison in that Decision.
_Avoid_: Timestamp, request time

**Replay**:
Re-running a past Decision from its Audit record and getting byte-identical output.
_Avoid_: Re-evaluation, re-decision

**Audit record**:
The append-only entry for one Decision: its full inputs (the request, Mandate snapshot, the Agent as it was, Spent this month and Decision time), the Policy pack and Pack hash, and the output with every Rule result.
_Avoid_: Log, event, history

**Mandate snapshot**:
A copy of a Mandate's terms stored inside an Audit record, so evidence never depends on looking the Mandate up again.
_Avoid_: Mandate reference

**Chain head**:
The position and hash of a Tenant's latest Audit record, which the next record must follow.
_Avoid_: Tip, last record

**Audit chain**:
A Tenant's Audit records linked in sequence, each carrying the hash of the one before, so any edit or gap can be detected.
_Avoid_: Ledger, blockchain, log

## Disputes

**Dispute**:
A Tenant's report that the Payer has challenged one captured Payment, usually as a chargeback through their card issuer. A Payment has at most one Dispute. Opening one never changes the Payment or its Decision.
_Avoid_: Chargeback (that is the network process), complaint, claim

**Dispute reason**:
Why the Payer says the Payment was wrong, from a small fixed set (such as unauthorised or not as described). Each maps to card network reason codes and names the evidence it needs.
_Avoid_: Reason code (that is why a rule fired), chargeback code

**Evidence bundle**:
Every record a Dispute's pack is built from, gathered from the Audit chain and the Payment's rail history by fixed rules, never chosen by a model.
_Avoid_: Context, trail, case file

**Evidence pack**:
What the Tenant submits to contest a Dispute: the Evidence bundle, the network reason codes, the Evidence strength and a narrative whose every claim cites an Audit record. A pack is ready only once a person signs it off.
_Avoid_: Dispute pack (in prose), report, case

**Evidence strength**:
How well the Evidence bundle supports the charge for the Dispute reason: strong, mixed or weak. The Policy pack says, for each Dispute reason, which facts about the bundle make it strong and which make it weak, and from `default@1.5.0` which facts make it strong for every reason, such as Refunds of the whole captured amount (a credit already issued); it is never judged by a model.
_Avoid_: Score, confidence, win probability

**Sign-off**:
A named person at the Tenant approving one exact version of an Evidence pack, which marks it ready. The Tenant attests to who signed; AgentPay Guard does not verify it.
_Avoid_: Approval (that is for Consent requests), review

**Review link**:
An expiring link to one Dispute's Evidence pack, which the Tenant mints for a named reviewer. Whoever opens it is treated as that reviewer, and it is used up by Sign-off. A newer link for the same Dispute replaces it.
_Avoid_: Sign-off link, Consent link (that is for Consent requests)

## Quality

**Golden case**:
One hand-labelled input to the engine (or the Dispute rules, or the citation checker) with the answer it must give, written from the Policy pack's rules and never copied from the engine's output. A Golden case may name the build-plan scenario it covers.
_Avoid_: Test case, fixture, sample

**Eval report**:
The published numbers of one run over every Golden case: case counts per file and scenario, false-allow, false-block, wrong Reason codes and the step-up rate. It holds only numbers every machine reproduces, so latency is left out, and CI fails when the committed copy is out of date.
_Avoid_: Scorecard (that is the page that shows it), eval results, metrics

**Model comparison**:
A hand-run scoring of each model job's live eval (the Intent check and the dispute narrative) on several Claude models, three passes each, with the production prompt, effort and timeout. A case counts as right only if every pass got it right. Its committed report (`evals/model_comparison.md`) is what a choice of model cites. It costs money, so it never runs in CI and no staleness check covers it.
_Avoid_: Model benchmark, bake-off, Eval report (that is the golden-set run)

**Ops console**:
The web app's read-only scorecard page (`/ops`), built from the committed Eval report: case counts, false-allows, false-blocks, wrong Reason codes and the step-up rate, per scenario and per golden file, with the release it came from. It holds no live data and needs no API key.
_Avoid_: Dashboard, admin panel, ops dashboard

## Demo

**Scenario**:
One demo journey as data: a JSON file holding its id (from `docs/demo-journeys.md`), title, group, playlists, plain-English story, setup (Mandate terms, merchant, mock rail card, history), ordered steps with the chat lines said before each, Demo controls used, expected result, scripted model replies and "what to notice". Each step names its actor: an Agent step names a `guard_mcp` tool, a Tenant step an HTTP route, a Payer or reviewer step what the person does. CI runs every Scenario against the in-process app and checks its expected result.
_Avoid_: Journey script, demo case, Golden case (that is an engine input with its labelled answer)

**Scenario catalogue**:
Every Scenario file, one per journey, in `demo/scenarios/`, read by the web app, `python -m demo --scenario` and the Demo runner. Its format is one Pydantic model (`demo/scenario.py`) with a committed JSON Schema that CI checks for staleness.
_Avoid_: Journey list (that is `docs/demo-journeys.md`), scenario library

**Demo runner**:
The small server beside the API (`python -m demo_runner`) that plays Scenarios for the web app's /demo page, one step per browser request, each step answered with a card. It holds the Demo Tenant's keys so the browser never does, talks to the API over HTTP only, makes no model call of its own, grants each run its own Payer, Agent and Mandate, and is rate-limited per client IP and overall (ADR-0031).
_Avoid_: Demo server, demo backend, demo proxy
