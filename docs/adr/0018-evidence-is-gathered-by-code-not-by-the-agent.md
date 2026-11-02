# Evidence is gathered by code, and the model only writes the narrative

The dispute evidence agent does not choose what to read. Code gathers the Evidence bundle by fixed rules from the Audit chain and the Payment's rail history, hashes it, and makes one ModelGateway call that turns the whole bundle into a narrative of claims, each citing Audit record IDs. A checker rejects any claim whose citations are missing or not in the bundle. Network reason codes and Evidence strength are worked out by code, never by the model.

## Considered Options

- **A tool-using agent loop** (the model calls `get_audit_trail`, `get_mandate`, `get_consent_record` and `get_rail_events` as it sees fit), as docs/BUILD-PLAN.md first described. Rejected because the trail for one Payment is small and fully known in advance, so a search adds cost and lets the model skip evidence, and a pack built from a model-chosen set of records can't be rebuilt from the Audit chain. The bundle is built by readers with those four names, so a tool loop can sit behind them later without changing the pack.

## Consequences

- The same Payment always gives the same Evidence bundle and bundle hash; only the narrative depends on the model.
- A narrative the checker rejects, or a model call that fails, never blocks a pack: it falls back to a narrative written by code from the bundle.
