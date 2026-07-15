---
node: spec/bcp-0033-operator-bench
kind: bcp
edges:
  depends-on:
    - spec/bcp-0032-repository-operator
---

# BCP-0033: OperatorBench and Ablations

Status: contract complete — deterministic pilot and matched comparison workflow implemented;
live model effect remains unmeasured

Provide deterministic public scenarios, raw/latest-N/structured/term-retrieval/FTS5-retrieval
conditions, exact fixture grading, and a canonical paired report. The same decision-model instance
consumes every condition under one hard context ceiling and action schema; every full context,
proposal, invocation, score, and content identity is retained.

The recorded WP23 artifact validates that contract but does not estimate a live model effect.
Promotion of any intervention still requires the preregistered scenario and stochastic-replicate
minimums in the evaluation methodology.

Acceptance:

- scenarios cover staleness, conflicts, missing evidence, distractors, corrections, blocked
  dependencies, partial failure, and unsafe proposals;
- fixture metrics cover outcomes, evidence visibility, policy, context size, and latency;
- Repository Operator tests independently cover artifact and projection replay integrity;
- recorded fixtures run without credentials or network access;
- live-model trials remain explicitly separate from deterministic CI and require a pinned model,
  at least three replicates, and an exclusively reserved artifact path;
- paired Wilson and bootstrap uncertainty, limitations, and the non-promotion decision are
  retained in `docs/decisions/runtime-v1/wp23-context-retrieval.json`.
