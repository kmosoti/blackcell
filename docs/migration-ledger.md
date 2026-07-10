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

Status uses three evidence levels:

- **contract complete**: typed behavior and its focused invariants exist;
- **integrated**: production composition, persistence, and adjacent feature behavior are verified;
- **product accepted**: the public workflow satisfies its end-to-end acceptance evidence.

| Package | Current evidence | Remaining acceptance |
| --- | --- | --- |
| WP00-WP02 | accepted support deliverables; no product maturity claim | Keep the debt and status records current. |
| WP03 | integrated kernel append/replay baseline | Durable run, execution-journal, and DAG records remain. |
| WP04a-WP04b | contract complete; ingestion feeds the new projector | Add domain scope, explicit missing-state semantics, and legacy-projector parity tests. |
| WP05a-WP05b | contract complete; composed in memory | Persist/inspect frames and distinguish missing required evidence from other omissions; FTS5 remains pending. |
| WP06a-WP06b | contract complete | Route the workflow through the gateway and durably record requests, responses, failures, retries, and usage. |
| WP07a-WP07b | contract complete; composed in memory | Persist proof/authorization artifacts; Clingo parity is pending. |
| WP08 | contract complete with execution-identity collision checks | Add SQLite journal, real bounded adapter, timeout/isolation, and kernel events. |
| WP09a | control-path skeleton only | Add gateway composition, trace, re-observation, evaluation, transition commit, replay, and acceptance scenarios. |
| WP09b | pending | Make Repository Operator and CLI delegate after characterization. |
| WP10-WP12 | pending | Begin only after recorded outcome/transition data and replay exist. |
| WP13-WP15 | pending | Simulate DAG invariants before durable leases/fencing and role binding. |
| WP16-WP17 | pending; promoted ahead of WP10 | Close outcome evaluation and live-free replay first. |
| WP18-WP22 | pending | Define security boundary, then API, Granian, OTel, Podman, and recovery. |
| WP23-WP27 | pending | Run matched experiments, reliability work, retirement, and release evidence last. |

The dependency-correct execution sequence and branch/review protocol are canonical in
`docs/spec/bcp-0034-evolutionary-runtime.md`.

## Non-negotiable migration rules

1. Every production state change is an append to the kernel event ledger.
2. A legacy path is deleted only after its replacement passes characterization and replay tests.
3. Replay has no dependency path to live model, harness, network, or affordance execution.
4. Model reasoning, coding, and embedding are selected by gateway policy, not agent-owned SDK code.
5. Predicted state is never silently committed as observed state.
6. Symbolic denial dominates a neural proposal unless an explicit, audited human approval policy
   applies.
7. Each work package is one reviewable commit and leaves the full verification suite green.
