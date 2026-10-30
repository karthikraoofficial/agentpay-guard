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
What a Tenant's API key may do: decide (Agent traffic), consent (resolving Consent requests) or manage (granting Mandates and registering or switching Agents and Payers). An Agent is only ever given a decide key.
_Avoid_: Role, permission

**Disabled agent**:
An Agent its Tenant has switched off. Its requests are blocked under every Mandate until it is re-enabled.
_Avoid_: Revoked agent, suspended agent

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
The sum of allowed payments and approved step-ups under a Mandate in the current calendar month (UTC), less any that were Voided, checked against the Monthly cap. An approved step-up counts in the month it was approved, not the month it was requested.
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
The money movement for one allowed Decision or approved step-up, for exactly its amount. A Decision has at most one Payment. A Payment is scheduled from the moment it is made, and becomes captured or failed when the rail reports back, or failed at once if the rail refuses it.
_Avoid_: Transaction, Agent payment request (that is only the ask)

**Rail charge**:
The rail's own record of the debit for one Payment: what the rail says happened, which a Payment follows.
_Avoid_: Payment (that is our record), transaction

**Payment window**:
How long an Agent has to start its Payment, counted from the allow or from the approval of a step-up, and set in the Policy pack. After it, the Decision can no longer be paid.
_Avoid_: Expiry, TTL

**Void**:
Releasing an allow or approved step-up from Spent this month, because its Payment failed or the Tenant cancelled it before paying. Recorded as a new Audit record and never by changing the Decision.
_Avoid_: Cancellation, reversal, refund (a refund returns captured money)

**Tenant webhook**:
A signed notice that AgentPay Guard sends to a Tenant's own endpoint when something it cares about happens, such as a Consent request being created or a Payment being captured. Delivered at least once.
_Avoid_: Callback, notification (that is what the Tenant sends the Payer), rail webhook

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
