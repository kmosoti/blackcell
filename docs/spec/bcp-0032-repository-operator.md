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

Status: product accepted — public facade delegates to canonical Daily Operator v2; predecessor retired

Connect observation ingestion, state projection, ContextFrame construction, model proposal,
policy evaluation, one bounded affordance, outcome observation, evaluation, and event append.

The public `RepositoryOperator` composes `DailyOperatorV2Workflow`, the model gateway, typed
repository status adapters, canonical state/context inspection, and read-only replay. The
characterized predecessor and its `operator.*` writer were removed by WP26. Immutable
`daily-operator/v1` history remains readable only through the live-free replay adapter.

Acceptance:

- the recorded gateway route deterministically derives a schema-valid baseline proposal from the
  admitted request, while the optional Codex route requires an explicit model ID and uses the
  bounded `CodexCliModelAdapter`;
- one fixed `inspect_repository` execution reads Git status without a shell, and a distinct status
  read supplies post-execution outcome evidence;
- one CLI command completes the loop and emits machine-readable output;
- state, context, and replay inspection use the canonical runtime-v1 contracts;
- human corrections append `observation.corrected` evidence through `IngestCorrectionHandler`;
- the model never gains direct execution authority.
