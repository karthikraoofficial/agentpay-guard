# One audit hash chain per Tenant, written in the same transaction as the Decision

Each Tenant's Audit records form their own hash chain: `record_hash = SHA-256(prev_hash || canonical_json(record))`, and a Tenant's first record uses 32 zero bytes as its `prev_hash`. A per-tenant chain-head row is locked for each append, so `seq` and `prev_hash` cannot fork. The Decision, its Audit record and its idempotency entry commit in one transaction. A Decision without an Audit record never exists.

Each record is stored as its exact canonical JSON bytes, and the hash is taken over the previous record's 32 raw hash bytes followed by those bytes, so verification and replay never re-serialise anything. Postgres triggers reject UPDATE, DELETE and TRUNCATE on `audit_records` for every role, including the table owner. We chose triggers over revoking grants because grants don't bind the owner role that migrations and local development connect as.

We rejected a single global chain because every Decision across all Tenants would serialise on one lock, and one Tenant's evidence could not be exported and verified without the others'. We rejected writing the Audit record asynchronously through the outbox because there would then be a window in which a Decision had been returned but not recorded.
