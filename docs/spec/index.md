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
    - spec/bcp-0034-evolutionary-runtime
  informs:
    - charter
    - architecture
---

# Blackcell Specifications

## Landed Phase 1 foundation

1. `BCP-0028`: charter and terminology reset.
2. `BCP-0029`: unified event and artifact kernel.
3. `BCP-0030`: repository evidence and state projection.
4. `BCP-0031`: context projection and symbolic control.
5. `BCP-0032`: legacy Repository Operator acceptance surface.
6. `BCP-0033`: deterministic OperatorBench fixture pilot.

## Active runtime-v1 program

7. `BCP-0034`: continuous migration from the accepted legacy product into one canonical
   gateway-backed, evaluated, replayable runtime, followed by durable DAG orchestration, API,
   deployment, matched experiments, and release evidence.

Phase 1 product acceptance ends when the Repository Operator and CLI delegate to the canonical
closed loop and its live-free replay path. Multi-agent DAG, API, container, and research acceptance
are runtime-v1 program work after that join, not additional Phase 1 requirements.

BCP-0026 and BCP-0027 describe the superseded July 6 prototype. Their generic SQLite and
feature-baseline lessons are preserved, but the current architecture no longer treats a
deterministic feature sketch as a latent world model.
