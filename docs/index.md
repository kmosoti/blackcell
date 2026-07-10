---
node: index
kind: atlas-entry
edges:
  maps:
    - atlas/graph
  introduces:
    - charter
    - architecture
    - scientific-basis
    - evaluation-methodology
    - spec/index
---

# Blackcell Documentation

## Canonical documents

- `charter.md`: identity, scope, claim gates, and Phase 1 acceptance criteria
- `scientific-basis.md`: terminology and research promotion rules
- `architecture.md`: event, projection, model, policy, execution, and replay boundaries
- `evaluation-methodology.md`: OperatorBench conditions, measures, and trial protocol
- `adr/`: accepted architectural decisions

## Phase 1 specifications

- `spec/bcp-0028-charter-reset.md`
- `spec/bcp-0029-event-kernel.md`
- `spec/bcp-0030-repository-state.md`
- `spec/bcp-0031-context-and-control.md`
- `spec/bcp-0032-repository-operator.md`
- `spec/bcp-0033-operator-bench.md`

## Prototype archive

Documents under `concepts/`, `guides/latent-harness-quickstart.md`, `research/`, and the older
BCP-0026/0027 specifications describe the July 6 prototype. They are retained temporarily
for migration history and are not the current architecture contract.
