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
    - guides/runtime-v1-release
    - spec/index
---

# Blackcell Documentation

## Canonical documents

- `charter.md`: identity, scope, claim gates, accepted Phase 1, and completed runtime-v1 boundaries
- `scientific-basis.md`: terminology and research promotion rules
- `architecture.md`: event, projection, model, policy, execution, and replay boundaries
- `evaluation-methodology.md`: OperatorBench, PredictionBench, and RuntimeBench contracts
- `implementation-baseline.md`: measured starting point and preservation boundaries
- `migration-ledger.md`: strangler map from current packages to target feature ownership
- `guides/runtime-v1-release.md`: executable runtime-v1 walkthrough and unpublished evidence bundle
- `adr/`: accepted architectural decisions

The current durability boundary is defined by
`adr/0005-durable-run-and-execution-protocol.md`: artifact-first causal run records and explicit
prepared-action recovery without whole-workflow or distributed exactly-once claims.

The service security boundary is defined by `adr/0007-runtime-security-boundary.md`: explicit
owner-only data paths, opaque credentials, strict Bearer/scope authorization, zero proxy trust, and
pre-storage redaction before HTTP exposure.

Architecture consolidation is governed by `adr/0008-architecture-consolidation.md`: boundaries are
retained or consolidated from direct authority, failure, persistence, substitution, and security
evidence rather than class, file, or import counts. Its source-bound AC00 baseline lives under
`decisions/architecture-consolidation/`.

The local recovery and quota runbook is `targets/recovery.md`: verified immutable bundles,
non-destructive cutover, verified-only retention, and the exact request/storage admission limits.

Runtime-v1 release evidence is complete and unpublished. The maintained guide is
`guides/runtime-v1-release.md`; the deterministic SBOM and verification manifest live under
`../release/runtime-v1/`.

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

Historical documents under `concepts/`, `research/`, and the older BCP-0026/0027 specifications
describe the July 6 prototype or later research questions. WP26 removed their executable packages,
commands, independent stores, and generated agent artifacts. The retained documents are not the
current architecture contract and do not define compatibility surfaces.
