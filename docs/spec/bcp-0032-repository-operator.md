---
node: spec/bcp-0032-repository-operator
kind: bcp
edges:
  depends-on:
    - spec/bcp-0031-context-and-control
  precedes:
    - spec/bcp-0033-operator-bench
---

# BCP-0032: Repository Operator Loop

Status: implemented

Connect observation ingestion, state projection, ContextFrame construction, model proposal,
policy evaluation, one bounded affordance, outcome observation, evaluation, and event append.

Acceptance:

- `RecordedModel` supports deterministic CI and replay;
- `CodexExecModel` is optional, structured, time-bounded, and uses the Codex CLI read-only
  sandbox from a temporary frame-only workspace;
- one CLI command completes the loop and emits machine-readable output;
- the model never gains direct execution authority.
