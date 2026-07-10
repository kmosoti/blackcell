---
node: spec/bcp-0030-repository-state
kind: bcp
edges:
  depends-on:
    - spec/bcp-0029-event-kernel
  precedes:
    - spec/bcp-0031-context-and-control
---

# BCP-0030: Repository Evidence and State Projection

Status: implemented

Define repository observations, provenance-bearing claims, epistemic status, effective and
observed time, freshness, conflicts, corrections, unknowns, and point-in-time operational
state estimation. Derive a content-addressed SignalPacket that summarizes current telemetry
without becoming model context or another state store.

Acceptance:

- repository, task, and check evidence become semantic events;
- source conflicts are preserved rather than overwritten;
- corrections produce new evidence and point-in-time projections remain reconstructable;
- stale and missing required evidence are explicit;
- SignalPackets remain provenance-linked and distinct from ContextFrames.
