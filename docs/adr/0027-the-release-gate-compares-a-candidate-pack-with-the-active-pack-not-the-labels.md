# The Release gate compares a Candidate pack with the active pack, not with the labels

Each golden file is pinned to the Policy pack it was labelled for (`phase1` on `default@1.0.0`, `phase2` on `1.1.0`, and so on), so the evals never run a new pack against the cases labelled for older ones (#234). The Release gate fills that gap. It decides every Decision and approval golden case on both the active pack and the Candidate pack, and compares the two answers: Outcome, Reason codes, Risk tier and the Consent resolution.

We considered gating on the labels: run every golden file on the candidate and require it to match. That can't work. Packs change answers on purpose. From `1.1.0`, a medium or high payment without an Intent check steps up, and on `1.5.0` every `phase1` case steps up for `VELOCITY_HIGH`, because those cases carry no Signals. Gating on labels would mean relabelling old golden files on every release, and a relabelled file stops testing the pack it was written for.

Comparing with the active pack shows only what this candidate changes. Changes that earlier releases made on purpose are already in the active pack's answers.

## Consequences

- **Old golden files keep their labels and their pins.** The evals still check each file against its own pack. The gate adds a second, pack-to-pack check over the same inputs.
- **Every change has to be accounted for.** An answer the candidate changes fails the gate unless it is declared (#237). A loosening on a case that is high on either pack always fails: block or step_up to allow, step_up to allow, or an approval that goes through where it was blocked.
- **The gate trusts the active pack.** It can't catch a regression already in the active pack. The labels catch those, through the evals, for files pinned to that pack.
- **The candidate's own golden files must still pass the eval thresholds.**
- **Two step-up rates.** The gate reports the step-up rate over every file decided on each pack, which differs from the Eval report's rate, decided on each file's pinned pack (46.7% and 40.1% on `default@1.5.0` today).
- **Golden inputs must stay decidable on every released pack.** Rule implementations already stay backward compatible with every released pack (ADR-0002). A case that can't be decided fails the gate and is named, never skipped.
