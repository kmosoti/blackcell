---
node: spec/index
kind: spec-entry
edges:
  maps:
    - spec/bcp-0028-charter-reset
    - spec/bcp-0029-event-kernel
    - spec/bcp-0030-repository-state
    - spec/bcp-0031-context-and-control
    - spec/bcp-0032-repository-operator
    - spec/bcp-0033-operator-bench
  informs:
    - charter
    - architecture
---

# Blackcell Specifications

## Current Phase 1 sequence

1. `BCP-0028`: charter and terminology reset.
2. `BCP-0029`: unified event and artifact kernel.
3. `BCP-0030`: repository evidence and state projection.
4. `BCP-0031`: context projection and symbolic control.
5. `BCP-0032`: Repository Operator loop.
6. `BCP-0033`: OperatorBench and ablations.

BCP-0026 and BCP-0027 describe the superseded July 6 prototype. Their generic SQLite and
feature-baseline lessons are preserved, but the current architecture no longer treats a
deterministic feature sketch as a latent world model.
