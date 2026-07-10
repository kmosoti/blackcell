---
node: migration-ledger
kind: architecture-ledger
edges:
  based-on:
    - implementation-baseline
  governed-by:
    - charter
    - architecture
  informs:
    - spec/index
---

# Evolutionary Runtime Migration Ledger

This ledger is the strangler map for moving the working repository operator and the research
prototype into one event-led runtime. Entries describe ownership and retirement; they do not
authorize a second implementation to become another permanent subsystem.

## Target ownership

| Current capability | Target owner | Migration rule |
| --- | --- | --- |
| `domains.repository.adapter` and repository events | `features/ingest_observation` | Preserve event meaning; emit through the kernel store. |
| `domains.repository.projector` and `models.projector` | `features/project_operational_state` | Make `OperationalStateEstimate` canonical. |
| `context.signals` | `features/derive_signal_packet` | Keep SignalPacket distinct from ContextFrame. |
| `context.projector` and context baselines | `features/build_context` and `features/retrieve_evidence` | Preserve independently inspectable context and citations. |
| `control.policies` | `features/authorize_action` and `features/solve_constraints` | Keep deterministic policy as the required baseline. |
| `control.executor` | `features/execute_affordance` plus execution adapters | No ambient tool authority. |
| `operator` | `workflows/daily_operator` | Retain `RepositoryOperator` as a delegating facade. |
| `models` | `gateway` plus model adapters | Providers are reachable only through the gateway port. |
| `agents` | gateway profiles and orchestration configuration | Render app-specific files only at compatibility edges. |
| `harness` | orchestration simulation and experiments | Keep deterministic simulation separate from production scheduling. |
| `world` and `latent` | `features/predict_transition` and experiments | Treat prediction as advisory until calibrated. |
| `nesy` | `features/solve_constraints` | Neural proposals never bypass symbolic validation. |
| `runtime` | execution adapters and bootstrap | Keep process/container concerns outside features. |
| `telemetry` | telemetry adapters | Preserve trace correlation and causal links. |
| `evaluation` | `features/evaluate_outcome` and experiments | Keep fixture grading distinct from comparative research claims. |
| `ledger` and `latent` SQLite stores | kernel compatibility adapters | Never dual write; retire after explicit migration evidence. |

## Stable target seams

```text
kernel -> features -> workflows -> interfaces/bootstrap
             ^              |
             |              v
          ports <- gateway/adapters
```

The dependency direction points toward policy and domain behavior. Litestar, Granian, SQLite,
Clingo, llama.cpp, OpenTelemetry, Podman, and provider SDKs are replaceable edge concerns.

## Work-package states

| Package | Outcome | State |
| --- | --- | --- |
| WP00 | Baseline, branch, migration ledger, preservation boundaries | complete |
| WP01 | Architecture decision record and package contracts | complete |
| WP02 | Executable dependency rules and shrinking debt manifest | complete |
| WP03 | Transactional kernel event store and replay contracts | complete |
| WP04a | Operational belief-state projection slice | complete |
| WP04b | Typed, provenance-aware observation ingestion | complete |
| WP05a | Telemetry-derived SignalPacket feature slice | complete on merge of this change |
| WP05b-WP09 | Retrieval, ContextFrame, gateway, control, DAG, compatibility | pending |
| WP10-WP15 | Predictive/NeSy experiments, evaluation, replay, simulations | pending |
| WP16-WP22 | HTTP runtime, Podman, observability, security, recovery | pending |
| WP23-WP27 | Benchmarks, migration completion, documentation, release | pending |

## Non-negotiable migration rules

1. Every production state change is an append to the kernel event ledger.
2. A legacy path is deleted only after its replacement passes characterization and replay tests.
3. Replay has no dependency path to live model, harness, network, or affordance execution.
4. Model reasoning, coding, and embedding are selected by gateway policy, not agent-owned SDK code.
5. Predicted state is never silently committed as observed state.
6. Symbolic denial dominates a neural proposal unless an explicit, audited human approval policy
   applies.
7. Each work package is one reviewable commit and leaves the full verification suite green.
