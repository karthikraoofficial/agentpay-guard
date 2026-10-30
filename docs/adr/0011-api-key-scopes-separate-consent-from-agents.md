# API keys carry scopes so an Agent cannot approve its own step_up

A Tenant can hold several API keys, each with a set of scopes:
- `decide` covers Agent traffic: asking for Decisions, Intent checks, and reads.
- `consent` covers resolving Consent requests, minting Consent links and listing Consent requests.
- `manage` covers the Tenant-side writes that grant or restore an Agent's power: registering Payers and Agents, granting Mandates and enabling or disabling Agents. It also covers reading Intent verdicts.

Revoking a Mandate is allowed with any scope, because it only removes power. `create-tenant` issues one `decide` key for Agents and one `consent` + `manage` key for the Tenant's own back end. Keys that existed before scopes get all three, so no integration breaks.

Before Phase 3, a Tenant had one key and its Agents called with it. If that key could also approve, a prompt-injected Agent could approve its own step_up, which would make step_up advisory. Under ADR-0008 the loss would also fall on the Tenant without anyone having warned it. A README warning to keep the key from Agents was rejected, because the whole point of step_up is that the Agent cannot clear it. _(Updated: #57)_ The same reasoning covers the Tenant-side writes. An Agent holding a key that can grant Mandates or re-enable Agents could hand itself a bigger Mandate, or switch itself back on after being disabled. That is why `manage` is a third scope rather than part of `decide`. It was kept separate from `consent` so that a Tenant can give its consent UI a key that cannot change Mandates.
