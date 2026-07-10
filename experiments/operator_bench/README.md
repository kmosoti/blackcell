# OperatorBench

OperatorBench is Blackcell's deterministic public fixture-contract pilot for the Repository
Operator vertical slice. It renders three context conditions while holding the task, action
schema, and grading logic fixed:

1. `raw-chronological`: every observation in sequence order.
2. `latest-n`: only the most recent N observations.
3. `structured`: an explicit state projection with provenance and unknowns.

The synthetic fixture set covers task dependencies, capacity and check state,
stale and conflicting observations, irrelevant distractors, human corrections,
partial tool failures, and unsafe proposals. The canonical fixtures live in
`blackcell.evaluation.scenarios`; `manifest.json` records the experiment contract.

Primary outcomes are task success, required-evidence recall, evidence precision, invisible
citations, unsupported claims, policy violations, false rejections, context/response size,
model token usage when reported, and latency. The deterministic command runs each fixture once;
its aggregate values are descriptive, not inferential estimates. A future paired comparative
experiment must run the same model on every condition, use a matched budget, and retain trial
artifacts and model metadata.

The deterministic grader is the initial baseline. A model judge may be added as a separately
reported secondary measure, never as a replacement for environment outcomes or exact policy
checks.
