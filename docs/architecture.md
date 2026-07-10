---
node: architecture
kind: architecture
edges:
  implements:
    - charter
  constrained-by:
    - scientific-basis
---

# Runtime Architecture

## Boundaries

Blackcell keeps one immutable evidence ledger and multiple domain-scoped projectors. A
repository, personal work queue, and telemetry system do not share one universal state
schema, transition model, action space, horizon, or objective. ContextFrames may compose
state estimates across domains, but prediction and control remain bounded by domain.

```mermaid
flowchart TD
    Sources[Observation sources] --> Ledger[Event and artifact ledger]
    Ledger --> Projector[Domain projector]
    Projector --> State[Operational state estimate]
    State --> Context[ContextFrame]
    Context --> Proposal[Model proposal]
    Proposal --> Gate[Policy and constraint gate]
    Gate --> Executor[Typed affordance executor]
    Executor --> Outcome[Outcome observation]
    Outcome --> Ledger
    Outcome --> Evaluation[Evaluation]
    Evaluation --> Ledger
    State --> Transition[Transition model]
    Transition --> Gate
```

## Command, event, projection, and artifact separation

Commands request work and use imperative names. Events record accepted facts in past tense.
Projections are rebuildable views. Artifacts are immutable, content-addressed payloads.

| Category | Examples |
| --- | --- |
| Command | `IngestObservation`, `BuildContext`, `RequestDecision`, `ExecuteAction` |
| Event | `ObservationRecorded`, `PolicyEvaluated`, `ActionSucceeded`, `OutcomeObserved` |
| Projection | `OperationalStateEstimate`, `SignalPacket`, `RunTrace` |
| Artifact | ContextFrame, model request/response, tool result, evaluation report |
| Definition | `AffordanceDefinition`, `ConstraintDefinition`, `EvaluationSpec` |
| Runtime instance | `ActionProposal`, `PolicyDecision`, `ActionAttempt`, `EvaluationResult` |

## Event envelope

Every event occurrence has a unique event ID and a stream-local sequence. The envelope also
contains event and schema versions, recorded and effective times, source and actor,
correlation and causation IDs, payload hash, and an optional idempotency key.

An idempotency key identifies a retried command, not an event's identity. Repeated equivalent
observations are still separate occurrences unless they are proven retries of the same
ingestion request.

Appending uses optimistic expected-sequence checks. Projectors record their version and last
processed global sequence. Projection tables are disposable and rebuildable.

## Artifacts

Large or sensitive ContextFrames, prompts, responses, tool output, and reports are stored as
content-addressed artifacts. Events contain hashes and metadata rather than duplicating
content. Artifact reads verify the digest before returning bytes.

## Replay modes

Historical replay reads recorded events, model results, tool results, and artifacts. It
recomputes deterministic projectors, policies, and graders and must reproduce their hashes.
It never calls a live model or repeats an external side effect.

Counterfactual rerun applies a current model, projector, policy, or grader to a historical
ContextFrame. It creates a new experiment and correlation ID. It is not deterministic replay.

## Action protocol

```text
ActionProposed
  -> PolicyEvaluated(allow | deny | require_approval)
  -> ActionPrepared(idempotency_key)
  -> ActionStarted
  -> ActionSucceeded | ActionFailed | ActionOutcomeUnknown
  -> OutcomeObserved
  -> EvaluationRecorded
```

SQLite and an external side effect cannot share one atomic transaction. After a crash with an
unknown outcome, the executor reconciles the side effect before retrying. Phase 1 avoids most
of this complexity by exposing only read-only affordances.

## Model boundary

A `DecisionModel` receives one serialized ContextFrame and a response schema. It returns a
typed proposal. It has no direct tool access and no ambient authority. Blackcell owns policy,
approval, execution, and outcome recording.

`RecordedModel` supports deterministic CI and replay. `CodexExecModel` is an optional local
adapter that runs in an isolated temporary Git workspace containing only the frame and schema.

## Observability boundary

Domain evidence and diagnostic telemetry remain separate. Stable internal spans include:

- `blackcell.run`;
- `blackcell.state.project`;
- `blackcell.context.build`;
- `blackcell.model.propose`;
- `blackcell.policy.evaluate`;
- `blackcell.affordance.execute`;
- `blackcell.outcome.evaluate`.

Span attributes contain low-cardinality identifiers and versions. Prompt and evidence content
is artifact data governed by an explicit redaction policy. OpenTelemetry mapping is an
exporter concern and cannot define the domain schema.
