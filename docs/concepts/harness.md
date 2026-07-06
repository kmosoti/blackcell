---
node: concepts/harness
kind: concept
edges:
  consumes:
    - concepts/world-model
    - concepts/nesy
  dispatches:
    - concepts/runtime-adapters
  produces:
    - concepts/traces
---

# Harness Runtime

The harness turns world state into an explicit plan and then dispatches that
plan through a runtime adapter.

## Core Objects

- `AgentSpec`: role, objective, sandbox posture
- `PlanStep`: a single step in the harness loop
- `HarnessPlan`: goal plus agents plus steps
- `RunTrace`: append-only record of what happened during dispatch

## First Slice

The first slice includes a dry-run runtime only. That keeps the architecture
visible before the project commits to automation depth.

```mermaid
sequenceDiagram
    participant R as Repo
    participant W as World Model
    participant N as NeSy Rules
    participant H as Harness
    participant A as Adapter
    participant T as Trace

    R->>W: observe
    W->>N: derive facts
    N->>H: validate constraints
    H->>A: dispatch plan
    A->>T: emit events
    T->>W: update state
```
