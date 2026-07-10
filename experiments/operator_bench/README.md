# OperatorBench

OperatorBench is Blackcell's deterministic public benchmark for the Repository
Operator vertical slice. It compares three context conditions while holding the
task, model configuration, action schema, and grading logic fixed:

1. `raw-chronological`: every observation in sequence order.
2. `latest-n`: only the most recent N observations.
3. `structured`: an explicit state projection with provenance and unknowns.

The synthetic fixture set covers task dependencies, capacity and check state,
stale and conflicting observations, irrelevant distractors, human corrections,
partial tool failures, and unsafe proposals. The canonical fixtures live in
`blackcell.evaluation.scenarios`; `manifest.json` records the experiment contract.

Primary outcomes are task success, required-evidence recall, evidence precision,
unsupported claims, policy violations, false rejections, context/response size,
model token usage when reported, and latency. Proportions are reported with 95%
Wilson intervals. Comparisons between context conditions should use paired trials
identified by `(scenario_id, replicate)`.

The deterministic grader is the initial baseline. A model judge may be added as
a separately reported secondary measure, never as a replacement for environment
outcomes or exact policy checks.
