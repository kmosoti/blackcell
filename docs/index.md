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
    - implementation-baseline
    - migration-ledger
    - spec/index
---

# Blackcell Documentation

## Canonical documents

- `charter.md`: identity, scope, claim gates, and Phase 1 acceptance criteria
- `scientific-basis.md`: terminology and research promotion rules
- `architecture.md`: event, projection, model, policy, execution, and replay boundaries
- `evaluation-methodology.md`: OperatorBench conditions, measures, and trial protocol
- `implementation-baseline.md`: measured starting point and preservation boundaries
- `migration-ledger.md`: strangler map from current packages to target feature ownership
- `adr/`: accepted architectural decisions

The current durability boundary is defined by
`adr/0005-durable-run-and-execution-protocol.md`: artifact-first causal run records and explicit
prepared-action recovery without whole-workflow or distributed exactly-once claims.

The service security boundary is defined by `adr/0007-runtime-security-boundary.md`: explicit
owner-only data paths, opaque credentials, strict Bearer/scope authorization, zero proxy trust, and
pre-storage redaction before HTTP exposure.

## Proposed developer-workflow research

- `research/spark-repository-perception.md`: matched Terra-versus-Spark repository-perception
  experiment using ephemeral, schema-validated, no-history worker packets. It is not a runtime
  contract.

## Phase 1 specifications

- `spec/bcp-0028-charter-reset.md`
- `spec/bcp-0029-event-kernel.md`
- `spec/bcp-0030-repository-state.md`
- `spec/bcp-0031-context-and-control.md`
- `spec/bcp-0032-repository-operator.md`
- `spec/bcp-0033-operator-bench.md`
- `spec/bcp-0034-evolutionary-runtime.md`

## Prototype archive

Other documents under `concepts/`, `guides/latent-harness-quickstart.md`, `research/`, and the
older BCP-0026/0027 specifications describe the July 6 prototype. They are retained temporarily
for migration history and are not the current architecture contract.
