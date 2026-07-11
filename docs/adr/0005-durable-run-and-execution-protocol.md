---
node: adr/0005-durable-run-and-execution-protocol
kind: adr
edges:
  decides:
    - architecture
    - migration-ledger
    - spec/bcp-0034-evolutionary-runtime
  depends-on:
    - adr/0001-event-sourced-kernel
    - adr/0003-model-execution-boundary
    - adr/0004-evolutionary-runtime-architecture
---

# ADR 0005: Artifact-First Runs and Prepared Affordance Execution

Status: accepted

## Context

The target Daily Operator composes state, context, proposal, constraint, authorization, and
execution contracts, but it does not yet record them as one durable causal run. Its current
execution journal also performs a read before an external call and saves the result afterward. A
process crash between the effect and the save can therefore cause a retry to execute the action
again.

SQLite can make local coordination durable, but it cannot share a transaction with an external
side effect or with the file-backed artifact store. The protocol must make those boundaries
explicit rather than claim distributed exactly-once execution or whole-workflow recovery.

## Decision

Use one append-only kernel stream per Daily Operator invocation and an adapter-owned SQLite
execution journal in the same kernel database.

### Run identity and delivery

- The caller supplies one semantic `run_id`; the stream is `daily-operator-run:{run_id}`.
- Every run event uses `correlation_id=run_id` and links to the previous committed run event through
  `causation_id`.
- The ingestion correlation ID must equal the run ID. The workflow owns internal causation.
- A run stream is create-only in this version. Reusing a terminal run ID raises `RunAlreadyExists`;
  finding a nonterminal stream raises `RunInterrupted`.
- An existing run is never silently sent to a live decision model or affordance again. Automatic
  workflow resumption remains deferred until model-call and scheduler recovery semantics exist.

### Run event grammar

The bounded version-one success path is:

```text
run.started
  -> run.context-recorded
  -> run.proposal-recorded
  -> run.constraints-evaluated
  -> run.authorization-decided
  -> run.execution-recorded?  # allowed actions only
  -> run.trace-recorded
  -> run.completed
```

A caught workflow exception records the durable prefix, then best-effort trace and `run.failed`
events. Denial and approval-required outcomes are completed safety decisions, not failures.
Execution results with `failed` or `unknown` status are also completed records; `unknown` requires
later reconciliation. Exactly one terminal event is allowed, and no event may follow it.

Gateway request and response events are not part of this version. They are added between context
and proposal only after the workflow uses the model gateway and can reference real gateway
artifacts.

### Artifact ownership and ordering

- ContextFrame, proposal, constraint evaluation and proofs, authorization, execution result, run
  trace, and bounded failure details use explicit canonical JSON codecs.
- Material event payloads contain verified artifact references and small identity/status fields,
  not duplicate object bodies.
- The ContextFrame store owns the canonical ContextFrame artifact. The execution journal owns the
  canonical execution-result artifact. The run recorder verifies and references both rather than
  writing competing copies.
- A writer commits and verifies an artifact before appending an event that references it. A crash
  may leave an inert orphan artifact; an event must never point at an artifact that was not
  committed.

### Prepared-action state machine

The execution journal replaces `get -> execute -> save` with an atomic claim and fenced
completion:

```text
ABSENT -> PREPARED(execute)
PREPARED -> in-progress
UNKNOWN -> PREPARED(reconcile)
PREPARED -> SUCCEEDED | FAILED | UNKNOWN
SUCCEEDED | FAILED -> return recorded result
```

Before calling an adapter, the handler binds the run, invocation, authorization decision, action,
affordance definition, adapter, and idempotency key and commits `PREPARED`. Unique invocation,
authorization, execution-identity, and idempotency keys reject collisions before the adapter is
called. Completion compares a claim token and fencing revision, so a superseded caller cannot
overwrite a recovery result.

An active prepared claim fails closed as in progress. Recovery is explicit: an operator may fence
an abandoned claim and obtain a reconciliation claim, after establishing that the original worker
is no longer active. Recovery calls `reconcile`, never `execute`. Automatic lease expiry belongs to
the later durable DAG scheduler and is not inferred here.

Terminal results are immutable and exact retries return them without touching the adapter.
Persisted `UNKNOWN` results retry through reconciliation. This protocol prevents blind replay but
does not claim external exactly-once effects; adapters still need observation, reconciliation, and
where possible downstream idempotency.

## Atomicity and durability limits

- An individual event append or batch is atomic in SQLite.
- Artifact storage, ContextFrame indexing, execution-journal transitions, and run-event appends use
  separate transactions in the bounded implementation.
- Run start and observation ingestion are not one transaction. A process crash can leave a
  detectable nonterminal run.
- An external effect and SQLite cannot be atomic. A crash after a prepared claim requires explicit
  recovery and adapter reconciliation.
- A committed journal result whose run event was not appended is recoverable evidence, but
  automatic run-event repair is deferred.
- The current WAL and file-write configuration establishes process-crash recovery. Sudden
  power-loss durability is not claimed until SQLite critical commits, directory fsync behavior,
  backup, and restore are tested as a system.
- Run ordering is enforced by the run-recorder adapter. Direct low-level EventStore writers remain
  responsible for respecting the aggregate grammar.

## Consequences

- Runs become inspectable causal records without adding another event ledger or a run table.
- Duplicate run delivery and action-identity collisions fail before live dependencies are called.
- Execution history survives process restart and preserves uncertain/reconciled transitions.
- Content-addressed orphan artifacts are an intentional repairable failure mode.
- Whole-workflow resume, gateway invocation recovery, worker leases, feedback-loop evaluation,
  live-free replay, mutating affordances, and power-loss claims remain separate acceptance gates.

## Rejected alternatives

- Persist the existing dictionary-style journal unchanged: it cannot close the pre-effect crash
  window or concurrent duplicate race.
- Automatically treat an old prepared action as abandoned: without worker ownership and leases,
  this can overlap a still-running effect.
- Store material JSON in run events or a second run table: this duplicates artifact ownership and
  creates another source of truth.
- Emit synthetic gateway events around the plain decision port: this would record an architecture
  that did not execute.
