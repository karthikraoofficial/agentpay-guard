# The Payer instruction stays out of the Audit chain; only its hash goes in

An Intent check's Payer instruction and cart are stored on the Intent check record, which is tenant-scoped and can be erased. A Decision's Audit record carries only what the Decision used: the `intent_id`, the Intent verdict and its failure cause, the model and prompt version, the Quoted total, and a SHA-256 of the canonical instruction and cart.

We chose this because Audit records can never be changed or deleted (ADR-0004), and a Payer instruction is free text that often contains personal data, such as names, addresses or what someone is buying for whom. Putting it in the chain would make it impossible to erase without breaking the chain. Replay needs only the verdict and the Quoted total, so it never depends on the text. A dispute pack can still show the text while it exists, and the hash proves the text was not changed after the Decision.

## Consequences

Once the text has been erased, a dispute can show that an Intent check ran and what it concluded, but not what the Payer asked for.
