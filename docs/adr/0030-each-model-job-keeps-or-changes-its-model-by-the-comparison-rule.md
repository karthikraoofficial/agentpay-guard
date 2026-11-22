# Each model job keeps or changes its model by the comparison rule

Two jobs call a model: the Intent check and the dispute narrative. Their models (Claude Haiku 5.5 and Claude Sonnet 5.5) were picked without being compared on our own labelled cases. The Model comparison (`python -m evals.compare_models`, #243) now scores both jobs' live evals on several models, three passes each, with the production prompt, effort and timeout. Its committed report is [evals/model_comparison.md](../../evals/model_comparison.md), with the raw numbers in `evals/model_comparison.json`.

The rule was fixed in the PRD (#243) before any numbers existed, so the choice can't be argued backwards from the results:

1. **Hard gates.** Across all passes, a candidate has zero injection false-matches (Intent check) or zero followed injections (narrative), and no more timeouts or unanswered cases than the current model.
2. Among the models that pass the gates, pick **the cheapest whose accuracy (Intent check) or acceptance (narrative) is within 1 case of the best**. A case counts only if the model got it right on every pass.
3. **The current model wins ties.**

## The run (2026-11-22, commit `c1c3919`, 3 passes)

**Intent check** (`intent_check@1`, effort low, 2,500 ms timeout, 43 cases, 19 of them injections):

| model | correct | injection false-matches | unanswered | timeouts | cost per pass | p95 latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Haiku 5.5 (current) | 43/43 | 0 | 0 | 0 | $0.0023 | 1,516 ms |
| Sonnet 5.5 | 37/43 | 0 | 3 | 3 | $0.0452 | 2,452 ms |
| Opus 5.5 | 27/43 | 0 | 16 | 36 | $0.0885 | 2,500 ms |

Sonnet and Opus fail the timeout gate: at low effort they often don't answer within 2.5 s, and an unanswered Intent check is an `unknown` verdict that steps the Decision up. Only Haiku passes, so **the Intent check keeps Claude Haiku 5.5**.

**Dispute narrative** (`dispute_narrative@4`, effort medium, 45 s timeout, 12 cases, 4 of them injections):

| model | accepted | injections followed | unanswered | timeouts | cost per pass | p95 latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Haiku 5.5 | 12/12 | 0 | 0 | 0 | $0.0121 | 8,203 ms |
| Sonnet 5.5 (current) | 12/12 | 0 | 0 | 0 | $0.1293 | 5,891 ms |
| Opus 5.5 | 12/12 | 0 | 0 | 0 | $0.3250 | 11,750 ms |

All three pass the gates and all accept 12/12, so all are within 1 case of the best. The cheapest is Haiku, at about a tenth of Sonnet's cost. **The dispute narrative moves from Claude Sonnet 5.5 to Claude Haiku 5.5.**

## Consequences

- **The settings default changes:** `dispute_narrative_model` is `claude-haiku-5-5`, as are `.env.example` and `.env.production.example`. A deployment whose `.env` sets `AGENTPAY_DISPUTE_NARRATIVE_MODEL=claude-sonnet-5-5` explicitly keeps Sonnet until that line is changed. The prompt, effort, timeout and Model spend cap are unchanged.
- **Older Evidence packs still name Sonnet** in their `narrative_attempts`. Each attempt records the model that wrote it, so nothing needs migrating.
- **The live dispute eval now costs about $0.012 a run.** The README, CLAUDE.md and the eval's own messages say so.
- **Haiku is slower than Sonnet at this job** (p95 8.2 s against 5.9 s). That's well inside the 45 s timeout, and the narrative is written off every request path, by the worker.
- **The evidence is small.** Twelve narrative cases, four of them injections. A new injection technique, or a prompt change, is a reason to run the comparison again before relying on this choice. The command costs about $1.80 for both jobs, and the rule above applies again unchanged.
- **Follow-ups not compared here:** other vendors (OpenAI, Google and others), each of which needs its own ModelGateway adapter and prices; and Claude Fable 5.2, which costs twice Opus's price, always thinks, and takes turns too long for the Intent check's 2.5 s timeout. Effort sweeps and longer timeouts are out of scope too. If no model passed the gates for a job, the next step would be a prompt change, not a weaker gate.
