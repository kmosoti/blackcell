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
  introduces:
    - guides/latent-harness-quickstart
---

# Harness Runtime

The harness turns world state into an explicit plan and then dispatches that
plan through a runtime adapter.

## Core Objects

- `AgentSpec`: role, objective, sandbox posture
- `PlanStep`: a single step in the harness loop
- `HarnessPlan`: goal plus agents plus steps
- `RunTrace`: append-only record of what happened during dispatch

The dry-run harness also emits a compact latent prediction summary. When run
with `--latent-db <path>`, it records the simulated latent transition in the
local SQLite ledger and uses prior ledger transitions to label confidence.
Use `--show-stats` with `--latent-db` to fold action-level latent stats into the
same dry-run output.

`--latent off|summary|record|stats` controls this behavior explicitly. The older
`--latent-db` and `--show-stats` flags remain compatible shortcuts for `record`
and `stats` behavior.

For copy-paste commands and ledger inspection, see
`../guides/latent-harness-quickstart.md`.

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
    H->>T: attach latent prediction summary
    T->>W: update state
```
