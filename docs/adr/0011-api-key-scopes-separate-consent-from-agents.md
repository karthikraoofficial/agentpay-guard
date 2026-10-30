# API keys carry scopes so an Agent cannot approve its own step_up

A Tenant can hold several API keys, each with a set of scopes. `decide` covers Agent traffic: Decisions, Intent checks and reads. `consent` covers resolving Consent requests and minting Consent links. `create-tenant` issues one key of each, and keys that existed before scopes get both, so no integration breaks.

Before Phase 3, a Tenant had one key and its Agents called with it. If that key could also approve, a prompt-injected Agent could approve its own step_up, which would make step_up advisory. Under ADR-0008 the loss would also fall on the Tenant without anyone having warned it. A README warning to keep the key from Agents was rejected, because the whole point of step_up is that the Agent cannot clear it.
