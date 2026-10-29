# Policy packs hold parameters, not logic, and are hashed as canonical JSON

A Policy pack is a YAML file that sets only parameters (tier bands, the step-up threshold, the high-risk MCC list, the cap-near ratio) and the order and on/off state of rules. Rule logic lives in Python in `core/policy`. We rejected an expression language in YAML because it would need its own interpreter, tests and sandboxing. It would also make "same pack, same decision" depend on interpreter versions.

A pack's hash is the SHA-256 of its parsed content serialised as canonical JSON (sorted keys, no whitespace, UTF-8), not of the raw file bytes. Comments and formatting can then change without minting a new hash, while any change to a value does. `policy_packs/manifest.json` records each released version's hash. The engine refuses to start, and CI fails, if a released file no longer matches.

## Consequences

A new kind of rule needs a code release as well as a new pack version. Replaying an old Decision needs both the old pack file and engine code that still understands it, so rule implementations must stay backward compatible with every released pack.

_(Updated: #38)_ Optional sections that a pack doesn't set, such as Signal parameters added after a pack was released, are left out of the canonical JSON before hashing. Adding an optional section to the pack model therefore never changes a released pack's hash.
