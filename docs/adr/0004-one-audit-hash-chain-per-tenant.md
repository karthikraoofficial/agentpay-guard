# One audit hash chain per Tenant, written in the same transaction as the Decision

Each Tenant's Audit records form their own hash chain: `record_hash = SHA-256(prev_hash || canonical_json(record))`, and a Tenant's first record uses 32 zero bytes as its `prev_hash`. A per-tenant chain-head row is locked for each append, so `seq` and `prev_hash` cannot fork. The Decision, its Audit record and its idempotency entry commit in one transaction. A Decision without an Audit record never exists.

We rejected a single global chain because every Decision across all Tenants would serialise on one lock, and one Tenant's evidence could not be exported and verified without the others'. We rejected writing the Audit record asynchronously through the outbox because there would then be a window in which a Decision had been returned but not recorded.
