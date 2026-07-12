---
node: adr/0006-versioned-run-feedback-protocol
kind: adr
edges:
  decides:
    - architecture
    - migration-ledger
    - spec/bcp-0034-evolutionary-runtime
  depends-on:
    - adr/0001-event-sourced-kernel
    - adr/0003-model-execution-boundary
    - adr/0005-durable-run-and-execution-protocol
---

# ADR 0006: Versioned Gateway and Feedback Run Protocol

Status: accepted

## Context

ADR 0005 deliberately froze a bounded Daily Operator version-one grammar without gateway,
outcome, evaluation, or state-transition events. That stream is already durable public evidence
and cannot be reinterpreted in place. Runtime-v1 must add a real gateway-backed decision path and
close the feedback loop while preserving exact version-one replay.

The existing decision port accepts only a ContextFrame. It cannot carry complete request identity,
classification, locality, determinism, asymmetric budgets, causation, or node identity. The legacy
Repository Operator also lets model-authored expectations and executor-reported effects influence
evaluation. Those boundaries are not strong enough for the canonical runtime.

## Decision

Keep `daily-operator/v1` immutable and introduce `daily-operator/v2`. One public replay use case
dispatches by the recorded workflow version. New behavior is added through feature-owned ports and
artifact codecs; the workflow never imports a provider client or concrete observer.

### Version-two grammar

```text
run.started
  -> run.evaluation-specified
  -> run.initial-state-recorded
  -> run.context-recorded
  -> run.model-requested
  -> run.model-attempt-recorded
  -> run.model-responded
  -> run.proposal-recorded
  -> run.constraints-evaluated
  -> run.authorization-decided
```

The alternatives after `run.model-requested` are:

- exactly one fenced attempt followed by `run.model-responded`;
- `run.model-failed` with no attempt for pre-attempt admission or routing rejection;
- zero or one completed attempt followed by `run.model-failed` for terminal gateway
  failure.

Version two deliberately supports one attempt because its journal fails closed after an uncertain
interruption. Retries require a new run or future protocol version. The attempt and terminal events
together link exact route identity, status, latency, known usage or failure, and no secret content.
Admission rejection, missing route, adapter failure, enforced timeout, output-schema rejection,
budget overrun, and uncertain interruption are distinct typed failure categories. A post-call
latency measurement is evidence, not a deadline; concrete adapters enforce their own timeout. An
attempt-only prefix is uncertain and cannot be converted into a clean generic run failure without
durable gateway reconciliation.

Authorization branches continue as follows:

```text
DENY | REQUIRE_APPROVAL
  -> run.evaluation-recorded
  -> run.trace-recorded
  -> run.completed

ALLOW + UNKNOWN execution
  -> run.execution-recorded
  -> run.evaluation-recorded
  -> run.trace-recorded
  -> run.completed(requires-reconciliation)

ALLOW + SUCCEEDED | FAILED execution
  -> run.execution-recorded
  -> outcome observation events on the domain stream
  -> run.outcome-observed
  -> run.outcome-state-recorded
  -> run.evaluation-recorded
  -> run.state-transition-recorded?
  -> run.trace-recorded
  -> run.completed
```

A transition event is optional because an inconclusive observation has no accepted claims.
Evaluation failure does not erase independently observed facts: evaluation records whether the
goal was achieved, while a transition records which evidence changed operational state.

This is the run-stream grammar. Initial domain observation events occur after `run.started` and
are cited by `run.initial-state-recorded`. Every run-stream event retains immediate-predecessor
causation. Domain outcome events are caused by `run.execution-recorded`;
`run.outcome-observed` carries their event IDs as provenance
without breaking the run-stream chain.

`run.started` and `run.evaluation-specified` are appended atomically from one verified, immutable
Daily Operator request. The start event links that complete request artifact, and the evaluation
event links its nested EvaluationSpec. This makes the specification structurally mandatory on every
v2 branch, including eventual model failure or denial. `run.initial-state-recorded` is required once
before context and its state scope/cutoffs must equal the request and ContextFrame source-state
identity.

`run.outcome-state-recorded` exists only after independent observation for SUCCEEDED or FAILED
execution. Its cutoff includes every cited outcome observation. A transition binds the initial and
outcome state snapshots, action and execution identity, evaluation, and exact triggering evidence.
State artifacts are content-addressed snapshots for inspection and replay, not another mutable
state store.

### Request-decision boundary

Add a `request_decision` vertical slice. Its command owns run, node, request and causation
identity, classification, locality, determinism, token/latency/cost budgets, the ContextFrame
reference, and the exact ActionProposal output schema.

Decision preparation is separate from invocation:

1. Build and validate an immutable model request.
2. Commit and verify its artifact.
3. Append `run.model-requested`.
4. Invoke the gateway through the feature port.
5. Link the journal-owned route and attempt evidence before invocation, then link the durable
   terminal response or failure and any known usage evidence.
6. Decode and validate the ActionProposal before recording it.

The gateway may select a provider profile but cannot grant tools or affordances. Provider and model
names remain deployment configuration. A crash during a call leaves an interrupted durable prefix;
version two does not automatically resume or charge another model call.

### Independent outcome evidence

The developer-owned `EvaluationSpec` is part of the canonical Daily Operator request and request
digest. The model cannot author, alter, or waive it. An outcome observer receives only the targets
to inspect, never expected values. Its immutable observation is independently persisted and then
converted by the workflow into ordinary observation events whose ledger identity Blackcell owns.

Executor-reported effects may be supplemental evidence but cannot satisfy an independent
observation criterion. Denied, approval-required, and UNKNOWN executions do not call the observer.

### Artifact ownership

- `request_decision` owns model request, route, attempt, response, failure, and usage codecs and
  durably persists those artifacts in its attempt journal.
- `execute_affordance` owns execution preparation/result codecs and durably persists those
  artifacts in its execution journal.
- `project_operational_state` owns initial and outcome state artifacts.
- `observe_outcome` owns the observer result artifact.
- `evaluate_outcome` owns EvaluationSpec and evaluation artifacts.
- `accept_state_transition` owns transition artifacts.
- The run recorder verifies and links owner artifacts; it never writes competing payloads.

Artifacts commit and verify before their referencing event. An interruption may leave an inert
orphan artifact or a nonterminal run prefix, never a fabricated completed stage.

### Activation

Freezing this ADR, codecs, and validators does not activate v2 or change the current v1 recorder
default. A run chooses its workflow version at `run.started` and never upgrades mid-stream.

The v2 writer becomes selectable only when EvaluationSpec, initial-state snapshot, gateway
request/attempt/terminal evidence, independent observer, outcome-state snapshot, evaluation,
optional transition, strict v2 validation, and v2 replay verification are composed. The public
facade switches only after live-free replay and compatibility characterization. Blackcell never
emits a gateway-only or feedback-partial v2 history.

### Replay

`ReplayRun` has only history-reader, artifact-verifier, protocol-decoder, and
projection-verifier ports. Its constructor has no gateway, model, executor, observer, evaluator,
clock, or network dependency. Replay:

- validates both v1 and v2 grammar, correlation, causation, and artifact ownership;
- verifies cross-artifact identities and recorded state cutoffs;
- reports v1 state artifacts as not recorded rather than failed;
- classifies terminal, failed, interrupted, and corrupt histories;
- performs no write or live call.

Counterfactual execution creates a new run or experiment identity and is never historical replay.

## Consequences

- Existing v1 evidence remains stable and replayable.
- Gateway cost, failure, retry, and routing evidence becomes causal run data.
- Success criteria and outcome evidence no longer come from the proposing model or executor.
- The closed loop can produce transition samples suitable for deterministic prediction baselines.
- Whole-workflow resume, automatic retry after uncertain interruption, and distributed
  exactly-once execution remain unclaimed.

## Rejected alternatives

- Mutate the v1 grammar: this would change the meaning of committed event history.
- Let a gateway adapter invent run metadata: request policy belongs to the application command.
- Emit gateway events around the old `propose(frame)` port: that would fabricate a boundary the
  workflow did not execute.
- Treat executor output as observed reality: an executor cannot independently verify its effect.
- Require evaluation success before accepting observed facts: goal failure does not make evidence
  false.
