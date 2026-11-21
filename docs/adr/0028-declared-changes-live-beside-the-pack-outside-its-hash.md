# Declared changes live beside the pack, outside its hash, and ship with it

A Candidate pack that changes any golden answer on purpose says so in a declarations file beside its pack file, `policy_packs/<id>/<version>.declared-changes.yaml` (#237). Each entry names the golden file and case id, the answer on the active pack, the answer on the candidate, and a reason. The Release gate reads the candidate's file automatically and passes only when the changes it finds are exactly the ones declared, with matching answers (ADR-0027).

We considered putting the declarations inside the pack YAML. That would make them part of the Pack hash, so an Audit record's pack hash would change with prose that never touches a Decision, and the pack model would carry data the engine never reads. A separate file keeps the Pack hash, the engine and Replay exactly as they were.

The file is immutable once released, like the pack. CI's released-packs check (`python -m evals.released_packs`) fails a change that modifies, renames or deletes a declarations file already on the base branch. It also fails a declarations file added without its pack in the same change. That second rule may be surprising: you can't go back and explain an earlier release's changes. Allowing it would let anyone rewrite the reason for a change after it shipped. The reason has to be written when the change is reviewed.

## Consequences

- **The schema is generated from the model, like the pack schema.** `policy_packs/declared-changes.schema.json` comes from `evals.release_gate.Declarations` (`uv run python -m evals.release_gate --write-schema`), and a test fails if it's stale.
- **Declarations are tied to one active pack.** Each entry gives the answer on the active pack, so if the active version changes before the candidate merges, the entries go stale and the gate fails until the author fixes them.
- **Declaring a change doesn't make it safe.** A high-risk loosening fails the gate even when declared.
- _(Added: #259)_ **Pack windows are declared in their own list.** The Consent TTL, Payment window and Refund window are read by the API, so no golden answer shows them. The gate compares each pack's value directly, with the legacy default applied, so a pack that starts setting the default isn't a change. A change is declared in a `windows:` list beside `changes:`, as `{window, active, candidate, reason}` in the field's unit. A longer Consent TTL or Payment window keeps an allow or approval usable for longer, so it is flagged as a loosening, but it passes when declared. Lengthening one can be legitimate, for example for a slower rail. The Refund window has no looser direction: a longer one helps the Payer and a shorter one protects the merchant.
- **Packs released before the gate have no declarations file.** The gate treats a missing file as declaring nothing.
