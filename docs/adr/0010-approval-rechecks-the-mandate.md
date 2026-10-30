# Approval re-runs the engine; a Payer can lift a step_up but never a block

When a Payer approves a Consent request, the pure policy engine runs again with fresh inputs under the Mandate lock: the current Mandate snapshot, the Agent, Spent this month and the time of approval, plus the Signals recorded with the original Decision. If the engine now blocks, for example because the Mandate was revoked, it expired, the Agent was disabled or the Monthly cap was used up while the request waited, the approval is recorded but the Consent resolution is `blocked`, with the new Reason codes. Approval overrides step_up reasons only.

A simpler design would treat approval as turning the step_up into an allow. We rejected it because the wait can last minutes. A revocation in between would be ignored, and two parallel step_ups, each within the cap, could overspend it together. The resolution record carries its own inputs, so it replays like a Decision.
