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
| `domains.repository.projector` and `models.projector` | `features/project_operational_state` | Keep `OperationalBeliefState` canonical. |
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
| WP03 | integrated kernel append/replay baseline plus one causal Daily Operator run stream | Downstream replay and DAG packages consume this kernel; they are not remaining WP03 work. |
| WP04a-WP04d | integrated: ingestion, correction, effective-time replay, expiry, epistemic unknowns, exact scoped snapshots, and disposable incremental checkpoints | Public product inspection remains part of the compatibility cutover; no state-contract work blocks the canonical workflow. |
| WP05a-WP05c | integrated in `DailyOperatorV2Workflow`: scoped claim lineage, complete typed dispositions, bounded model payload, artifact-backed ContextFrame evidence, and exact state-to-context replay | Product-facing context inspection lands with WP09b. FTS5 remains a later matched retrieval intervention. |
| WP06a-WP06f | integrated: capability policy, request-decision contracts, durable fenced attempts, verified success/failure/usage evidence, gateway bridge, canonical workflow composition, and a deadline/output-bounded Codex CLI host-model adapter with no tool authority | WP09b now selects the recorded or explicit Codex route through gateway policy; no model-gateway work remains for the Phase 1 product. |
| WP07a-WP07b | integrated deterministic proof and authorization artifacts in the causal run | Deterministic policy remains the semantic reference and product default after WP12. |
| WP08a-WP08b | integrated prepared-action SQLite journal plus a real allowlisted local-process adapter with exact inputs, collision checks, timeout/isolation/output bounds, fencing, UNKNOWN reconciliation, and manual recovery | Durable scheduler-owned recovery remains WP13 work. |
| WP09a/WP09c | integrated create-only `daily-operator/v2` loop: complete request identity, gateway evidence, symbolic authorization, journaled execution, independent outcome evidence, deterministic evaluation, accepted transition, causal trace, and terminal safety outcomes | No remaining workflow-composition work blocks the public product path. |
| WP09b | product accepted: the public Repository Operator and JSON-first CLI delegate to `daily-operator/v2`, route recorded or explicit Codex decisions through the gateway, execute one bounded repository inspection, independently observe its outcome, expose canonical state/context/WP17 replay, and append corrections without dual writes | Retain the explicitly named legacy coordinator only until WP26 retirement evidence. |
| WP10 | integrated deterministic state-persistence prediction and later same-stream outcome scoring over canonical `OperationalBeliefState` snapshots, with typed unknown/conflict/missing findings, exact-match rate, Brier score, provenance, and content identity | Advisory DTOs have no event append or transition authority; learned or local-model claims require matched WP24 evidence. |
| WP11 | deferred by `docs/decisions/runtime-v1/wp11-local-predictor.json`: no installed offline runtime, configured prediction route, or matched WP10 evaluation exists | Add no speculative adapter or dependency; reconsider only after the recorded deployment, gateway-boundary, and WP24 comparison prerequisites are met. |
| WP12 | promoted by `docs/decisions/runtime-v1/wp12-clingo.json`: Clingo 5.8.0 imports on Python 3.14.6, is locked explicitly, and independently checks every decisive predicate through the feature-owned solver port | Exact deterministic proofs/explanations are returned on parity; drift or solver failure is content-free and fail-closed; workflow use requires explicit injection. |
| WP14 | integrated content-addressed DAG/node contracts, typed schema bindings, stable topological validation, gateway-bounded planner/executor/reviewer/verifier/synthesizer roles, retry/time/usage budgets, side-effect classes, and reviewer/verifier approval policy | Contracts reject cycles, missing edges, schema drift, forbidden capabilities/locality/classification, self-approval, and irreversible scheduler authority before submission. |
| WP15/WP13 | pending | Prove the WP14 invariants in deterministic failure simulation, then add the atomic session and durable SQLite scheduler. |
| WP16a-WP16c | integrated independent outcome evidence, deterministic evaluation, and evidence-scoped transition acceptance; inconclusive outcomes never commit state | No remaining WP16 work blocks the product join. |
| WP17 | integrated read-only `ReplayRun` contract and SQLite adapter for v1/v2 grammar, artifact ownership, journal identity, exact state cutoffs, terminal/interrupted/corrupt classification, and no-write replay | Product-facing replay selection and rendering remain part of WP09b delegation. |
| WP18-WP21 | pending | After the product use cases and scheduler stabilize, add API contracts, Granian lifecycle, Podman, and OTel. |
| WP22a-WP22b | pending | Security, auth, secrets, data-directory, and redaction boundaries precede API exposure; backup, restore, quotas, and recovery follow deployment. |
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
7. Each bounded work-package increment or review repair is one reviewable commit and leaves the
   full verification suite green.
8. Dependency joins trigger automated verification and independent review; they do not pause the
   continuous integration branch for routine user confirmation.
