---
node: adr/0004-evolutionary-runtime-architecture
kind: adr
edges:
  decides:
    - architecture
    - migration-ledger
  implemented-by:
    - spec/bcp-0034-evolutionary-runtime
---

# ADR 0004: Modular Monolith with Vertical Slices and an Event-Driven Kernel

Status: accepted

## Context

Blackcell must support state projection, context construction, model inference, symbolic control,
typed actions, evaluation, replay, and eventually durable multi-agent orchestration. The current
repository already proves most of the control loop, but technical-layer packages concentrate
coordination in a large operator service and leave experimental subsystems with overlapping state
and persistence concerns.

The next architecture must remain realistic on one machine, preserve deterministic tests, and
leave replaceable seams for local models, remote providers, Clingo, OpenTelemetry, Litestar,
Granian, and Podman. It must not require distributed infrastructure to demonstrate those seams.

## Options compared

| Criterion | A: layered Clean/Onion | B: pure vertical slices | C: distributed EDA | Selected hybrid |
| --- | --- | --- | --- | --- |
| Dependency control | strong | variable | strong across service contracts | strong, enforced inward |
| Feature locality | weak to moderate | strong | moderate | strong |
| Replay and event semantics | optional | duplicated easily | strong but operationally costly | strong in one kernel |
| Local operability | strong | strong | weak | strong |
| Independent evolution | moderate | strong | strong | strong within stable ports |
| Transactional consistency | strong | moderate | eventual by default | strong locally, explicit at edges |
| Infrastructure burden | low | low | high | low initially |
| Fit for research ablations | moderate | strong | weak to moderate | strong |

### A. Layered Clean/Onion architecture

Organize all domain entities, use cases, interfaces, and infrastructure into global layers. This
gives clear inward dependency direction, but a change to one behavior is scattered across layers
and the application layer tends to grow into another orchestration monolith.

### B. Pure vertical-slice architecture

Co-locate each command, handler, events, projection, and ports. This optimizes change locality, but
without a small kernel and dependency rules, slices can invent incompatible event envelopes,
persistence behavior, model clients, and policy semantics.

### C. Distributed event-driven services

Split ingestion, state, context, inference, policy, execution, and evaluation into independently
deployed consumers. This has strong isolation and scaling potential, but adds broker operations,
eventual-consistency failure modes, deployment coordination, and tracing complexity before the
runtime has workloads that justify them.

## Decision

Use a modular monolith that combines three patterns at different scales:

1. **Clean/Onion dependency direction** governs imports. Domain policy and feature handlers depend
   on kernel contracts and owned ports, never on frameworks or concrete adapters.
2. **Vertical slices** own behavior. Each feature co-locates commands, handlers, events,
   projections, and feature-specific ports.
3. **Event-driven architecture** governs state transitions. Accepted facts append to one kernel
   ledger; synchronous in-process dispatch is the initial delivery mechanism, and durable workers
   can be introduced without changing event meaning.

Workflows coordinate slices but do not contain domain policy. The model gateway and infrastructure
adapters implement feature-owned ports. Interfaces translate transport contracts into commands.
The composition root is the only place allowed to assemble concrete implementations.

## Package rule

```text
interfaces/bootstrap -> workflows -> features -> kernel
                              |          ^
                              v          |
                         ports <- gateway/adapters
```

Imports do not point left. Cross-feature communication uses stable contracts or recorded events;
one feature may not reach into another feature's handler or adapter internals.

## Runtime topology

The initial deployment is one process, one SQLite database, and content-addressed artifacts. A
rootless Podman container runs the same composition root through Granian and Litestar. Scheduler
and DAG durability use the kernel ledger and database rather than a broker. Broker or service
extraction requires measured contention, isolation, or availability needs and a separate ADR.

## Consequences

- Feature changes gain locality without losing common event and replay semantics.
- SQLite transactions can protect the authoritative local state transition.
- Framework and provider replacement stays at the edges.
- The architecture can simulate distributed boundaries without claiming distributed guarantees.
- Temporary compatibility facades are required while old package paths are strangled.
- Import rules and a debt manifest must be executable in CI; documentation alone is insufficient.

## Rejected shortcuts

- no dual event ledgers;
- no provider SDK imports in features or workflows;
- no direct action execution from a model or DAG node;
- no event broker, Kubernetes control plane, or graph database without measured need;
- no prediction result promoted to observation without an explicit accepted transition.
