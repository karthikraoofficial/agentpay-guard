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
A new Mandate that replaces an earlier, revoked one with changed terms.
_Avoid_: Mandate update, amendment

**Per-transaction cap**:
The largest single payment a Mandate permits.
_Avoid_: Limit, max amount

**Monthly cap**:
The most a Mandate permits in total across one calendar month (UTC), counting only allowed payments.
_Avoid_: Budget, rolling limit

**Allowed MCCs**:
The merchant category codes a Mandate permits. Anything not listed is outside the Mandate.
_Avoid_: Categories, whitelist

**Spent this month**:
The sum of allowed payments under a Mandate in the current calendar month (UTC), checked against the Monthly cap.
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
The Payer's approval or decline of a Consent request, or its expiry. Recorded as a new Audit record and never by changing the original Decision.
_Avoid_: Consent update, final decision

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
The merchant category code the Agent states in an Agent payment request, which is not yet confirmed by the rail.
_Avoid_: MCC (when the source matters)

**Agent reasoning**:
The Agent's own free-text explanation for a payment, kept as evidence and never used to decide.
_Avoid_: Justification, intent

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

**Audit chain**:
A Tenant's Audit records linked in sequence, each carrying the hash of the one before, so any edit or gap can be detected.
_Avoid_: Ledger, blockchain, log
