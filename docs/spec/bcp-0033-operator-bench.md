---
node: spec/bcp-0033-operator-bench
kind: bcp
edges:
  depends-on:
    - spec/bcp-0032-repository-operator
---

# BCP-0033: OperatorBench and Ablations

Status: implemented

Provide deterministic public scenarios, raw/latest-N/structured-context conditions,
deterministic graders, trial records, aggregation, and uncertainty summaries.

Acceptance:

- scenarios cover staleness, conflicts, missing evidence, distractors, corrections, blocked
  dependencies, partial failure, and unsafe proposals;
- metrics cover outcomes, evidence, policy, context size, and replay integrity;
- recorded fixtures run without credentials or network access;
- live-model trials remain explicitly separate from deterministic CI.
