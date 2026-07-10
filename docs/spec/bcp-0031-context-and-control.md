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

Status: implemented

Build immutable budgeted ContextFrames and separate affordance definitions, action proposals,
policy decisions, attempts, and observed outcomes.

Acceptance:

- selection, omissions, conflicts, unknowns, constraints, and affordances are inspectable;
- write or external mutation requires explicit approval;
- blocked dependencies, stale checks, and conflicting required evidence reject or redirect
  unsafe proposals;
- read-only execution is path-bounded and command-whitelisted.
