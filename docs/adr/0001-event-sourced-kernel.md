---
node: adr/0001-event-sourced-kernel
kind: adr
edges:
  decides:
    - architecture
---

# ADR 0001: Use an Event-Sourced Kernel for Agent Evidence

Status: accepted

## Decision

Use one local SQLite event store with immutable semantic events, optimistic stream versions,
separate idempotency keys, content-addressed artifacts, and rebuildable projections.

## Rationale

Point-in-time state, corrections, historical replay, prediction residuals, and execution
lineage are primary product requirements. An ordinary mutable state table would discard the
history needed to evaluate them.

## Consequences

Event schemas and projector versions require explicit evolution. Queries use projections
rather than scanning the event log. External side effects remain non-atomic and require
prepared-action and reconciliation semantics.

Kafka, a remote event service, and multiple writers are not justified in Phase 1.
