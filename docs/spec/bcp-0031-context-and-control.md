---
node: spec/bcp-0031-context-and-control
kind: bcp
edges:
  depends-on:
    - spec/bcp-0030-repository-state
  precedes:
    - spec/bcp-0032-repository-operator
---

# BCP-0031: Context Projection and Symbolic Control

Status: context/control contracts implemented; target context persistence integrated

Build immutable budgeted ContextFrames and separate affordance definitions, action proposals,
policy decisions, attempts, and observed outcomes.

Acceptance:

- selection, omissions, conflicts, unknowns, constraints, and affordances are inspectable;
- write or external mutation requires explicit approval;
- blocked dependencies, stale checks, and conflicting required evidence reject or redirect
  unsafe proposals;
- read-only execution is path-bounded and command-whitelisted.

ContextFrame v3 separates its bounded model-facing JSONL evidence payload from audit-only omission
bodies. Required gaps, retrieval omissions, and context-budget omissions are typed and
content-addressed; their composite claim identities form a complete, disjoint partition of the
source SignalPacket. The canonical frame encoding is stored once in the kernel ArtifactStore, with
`frame_id` equal to the artifact digest, before reasoning begins. The Daily Operator links that
artifact through `run.context-recorded` before model reasoning. The target CLI/API inspection path
remains pending.
