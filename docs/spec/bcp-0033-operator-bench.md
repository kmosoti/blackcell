---
node: spec/bcp-0033-operator-bench
kind: bcp
edges:
  depends-on:
    - spec/bcp-0032-repository-operator
---

# BCP-0033: OperatorBench and Ablations

Status: partial — deterministic fixture-contract pilot implemented; comparative experiment pending

Provide deterministic public scenarios, raw/latest-N/structured-context conditions, and exact
fixture-contract grading. Promote it to a comparative benchmark only after the same decision
model consumes each condition under a matched budget and its citations are graded against
visible evidence.

Acceptance:

- scenarios cover staleness, conflicts, missing evidence, distractors, corrections, blocked
  dependencies, partial failure, and unsafe proposals;
- fixture metrics cover outcomes, evidence visibility, policy, context size, and latency;
- Repository Operator tests independently cover artifact and projection replay integrity;
- recorded fixtures run without credentials or network access;
- live-model trials remain explicitly separate from deterministic CI.
