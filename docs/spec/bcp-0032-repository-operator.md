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

Status: legacy product accepted — canonical Daily Operator delegation remains pending

Connect observation ingestion, state projection, ContextFrame construction, model proposal,
policy evaluation, one bounded affordance, outcome observation, evaluation, and event append.

The current implementation proves the product behavior through `RepositoryOperator`. Runtime-v1
must characterize that behavior, make the facade delegate to the new vertical slices and gateway,
and retain historical replay compatibility before deleting the legacy coordination path.

Acceptance:

- `RecordedModel` supports deterministic CI and replay;
- `CodexExecModel` is optional, structured, time-bounded, and uses the Codex CLI read-only
  sandbox from a temporary frame-only workspace;
- one CLI command completes the loop and emits machine-readable output;
- the model never gains direct execution authority.
