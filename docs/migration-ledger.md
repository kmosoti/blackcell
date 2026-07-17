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

The dependency direction points toward policy and domain behavior. Litestar, Granian, Clingo,
llama.cpp, OpenTelemetry, Podman, and provider SDKs remain edge concerns. SQLite is the supported
trusted local kernel implementation for runtime-v1; alternate storage is not an interchangeable
backend and requires a separately approved deployment or implementation requirement.

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
| WP08a-WP08b | integrated prepared-action SQLite journal plus a real allowlisted local-process adapter with exact inputs, collision checks, timeout/isolation/output bounds, fencing, UNKNOWN reconciliation, and manual recovery | WP13 now owns automatic scheduler lease recovery; uncertain external execution remains explicitly reconciled through the WP08 journal. |
| WP09a/WP09c | integrated create-only `daily-operator/v2` loop: complete request identity, gateway evidence, symbolic authorization, journaled execution, independent outcome evidence, deterministic evaluation, accepted transition, causal trace, and terminal safety outcomes | No remaining workflow-composition work blocks the public product path. |
| WP09b | product accepted: the public Repository Operator and JSON-first CLI delegate to `daily-operator/v2`, route recorded or explicit Codex decisions through the gateway, execute one bounded repository inspection, independently observe its outcome, expose canonical state/context/WP17 replay, and append corrections without dual writes | WP26 removed the predecessor coordinator and v1 public writer; immutable v1 history remains readable through live-free replay only. |
| WP10 | integrated deterministic state-persistence prediction and later same-stream outcome scoring over canonical `OperationalBeliefState` snapshots, with typed unknown/conflict/missing findings, exact-match rate, Brier score, provenance, and content identity | Advisory DTOs have no event append or transition authority; learned or local-model claims require matched WP24 evidence. |
| WP11 | deferred by `docs/decisions/runtime-v1/wp11-local-predictor.json`: no installed offline runtime, configured prediction route, or matched WP10 evaluation exists | Add no speculative adapter or dependency; reconsider only after the recorded deployment, gateway-boundary, and WP24 comparison prerequisites are met. |
| WP12 | promoted by `docs/decisions/runtime-v1/wp12-clingo.json`: Clingo 5.8.0 imports on Python 3.14.6, is locked explicitly, and independently checks every decisive predicate through the feature-owned solver port | Exact deterministic proofs/explanations are returned on parity; drift or solver failure is content-free and fail-closed; workflow use requires explicit injection. |
| WP14 | integrated content-addressed DAG/node contracts, typed schema bindings, stable topological validation, gateway-bounded planner/executor/reviewer/verifier/synthesizer roles, retry/time/usage budgets, side-effect classes, and reviewer/verifier approval policy | Contracts reject cycles, missing edges, schema drift, forbidden capabilities/locality/classification, self-approval, and irreversible scheduler authority before submission. |
| WP15 | integrated deterministic failure simulation with bounded usage, retry, worker-loss, stale-completion, duplicate-delivery, approval, dependency-blocking, and stable report evidence | The pure simulator proves at-most-one simulated commit and fail-closed terminal behavior without dispatch, persistence, gateway, or ledger effects. |
| WP13a | integrated explicit SQLite kernel session and caller-owned in-transaction event append | Adapter DML and kernel events commit or roll back together; connection identity, active transaction, foreign keys, and transaction-control ownership are enforced. |
| WP13b | integrated restart-safe SQLite scheduler with canonical DAG reconstruction, independent approvals, dependency readiness, bounded leases/retries/backoff, monotonically fenced attempts, cumulative budgets, exact terminal idempotency, fail-fast branch fencing, and expired-worker recovery | Every accepted scheduler transition and terminal decision appends content-free causal kernel evidence in the WP13a transaction; concurrent claim, stale completion, restart, rollback, and corruption tests pass. |
| WP16a-WP16c | integrated independent outcome evidence, deterministic evaluation, and evidence-scoped transition acceptance; inconclusive outcomes never commit state | No remaining WP16 work blocks the product join. |
| WP17 | integrated read-only `ReplayRun` contract and SQLite adapter for v1/v2 grammar, artifact ownership, journal identity, exact state cutoffs, terminal/interrupted/corrupt classification, and no-write replay | Product-facing replay selection and rendering remain part of WP09b delegation. |
| WP18 | integrated versioned Litestar/msgspec API with raw-header Bearer/scope enforcement, bounded content-free failures, public liveness/readiness, strict observation/run/context/replay/evaluation/event/orchestration contracts, canonical use-case delegation, and owner-only SQLite creation | Run submission is synchronous; Granian owns process and worker lifecycle in WP19. |
| WP19 | integrated `blackcell-runtime` API/worker modes, bounded single-worker Granian ASGI lifecycle, pre-construction worker signal handling, reviewed five-role Repository Operator DAG, explicit handler allowlist, dependency/result artifact checks, usage enforcement, scheduler fencing, restart continuity, and replay-only verification | WP21 supplies OTel export and WP20 supplies container composition. No remote dispatcher or alternate state path was introduced. |
| WP21 | integrated opt-in OpenTelemetry OTLP/HTTP mapping over the canonical workflow telemetry port, with stable phase names, deterministic correlation, pre-export sanitization, bounded batching, content-free failure isolation, and API/worker lifecycle shutdown | No global tracer provider, ambient endpoint/header configuration, domain telemetry writes, collector, or container topology was introduced. |
| WP20 | integrated multi-stage development/runtime Containerfile, one numeric-non-root API/worker image, rootless Compose health sequencing, loopback host publication, read-only root and repository filesystems, dropped capabilities, no-new-privileges, runtime-only token injection, and persistent shared state volume | The explicit rootless engine gate proves health, UID/mode boundaries, secret exclusion, worker entry, read-only enforcement, and authenticated state survival across API restart. Collector topology and recovery remain separate. |
| WP22a | contract complete: explicit owner-only service data paths, exclusive environment-or-file opaque credential, strict Bearer authentication, explicit scope authorization, loopback/zero-proxy defaults, exact-value telemetry redaction, and ADR 0007 threat model | WP18-WP21 consume these contracts across HTTP, process, telemetry, and container edges. |
| WP22b | integrated consistent online SQLite-plus-artifact bundles, canonical manifest and exact inventory verification, owner-only fsync/rename publication, verified-only count retention, non-destructive restore, JSON-first recovery commands, global protected-request admission, active-storage reserve, and serialized exact artifact-byte ceiling | An external-copy disaster drill deletes the source state, restores a new canonical root, and proves artifact integrity plus live-free replay with the repository offline. Offsite transport, encryption, hard filesystem quotas, automatic cutover, and untested power-loss behavior are not claimed. |
| WP23a | contract complete and promoted for explicit experiments: ephemeral SQLite FTS5 ranking behind the feature-owned retrieval port, exact source-identity validation, shared required/conflict/unknown/omission policy, safe objective compilation, and content-free failure; the matched lexical fixture preserves evidence content and provenance at the same result and context budgets | The deterministic retriever remains the canonical workflow and replay default; no persistent index, dual write, graph/vector claim, or measured quality improvement is asserted. |
| WP23 | contract complete with a revise decision: one decision-model instance runs raw, latest-N, structured, deterministic-term, and FTS5 treatments under the same action schema and hard context ceiling; the canonical report retains 30 paired trial artifacts, Wilson summaries, paired bootstrap intervals, limitations, and content identities | The six-scenario recorded fixture is non-inferential and zeroes timing; no retrieval quality claim, product default, graph/vector adapter, or live-model effect is promoted. |
| WP24 | contract complete with continued deferral: eight matched synthetic scenarios compare state persistence with experiment-only developer-declared effects under the same advisory prediction/score schemas; the retained report covers exact match, Brier, source and actual missing/conflict, latency, tokens, cost, full identities, and environment | The deterministic baselines use zero model tokens and provider cost. Local-neural and hybrid candidates remain unavailable with null measures; no product default, learned-world-model claim, or neuro-symbolic-system claim is promoted. |
| WP25 | accepted reproducible baseline: six direct probes retain exact argv, environment and source fingerprints, output digests, per-test and subprocess timings, and a complete 28-pass rootless profile across API, worker, restart/fencing, quota, recovery, and container behavior | The single-host report is reliability evidence before optimization, not a service SLO, throughput result, production RTO/RPO, or default-change trigger. |
| WP26 | accepted retirement: a retained 106-test characterization precedes deletion of six prototype roots, two independent SQLite stores, predecessor operator/v1 writer paths, eight obsolete top-level CLI surfaces, and tracked OpenCode projections; architecture debt is empty and v1/v2 live-free replay remains | No aliases, tombstone commands, dual writes, stale production imports, or legacy public writers remain. Historical v1 grammar decoding is retained read-only. |
| WP27 | accepted release evidence: maintained operator/service documentation, isolated recorded-runtime examples, a deterministic CycloneDX 1.7 locked Python-runtime SBOM, and a complete material/command verification manifest | Runtime-v1 is evidence-complete and unpublished. No package, image, tag, release, signature, provenance attestation, vulnerability result, commit, or push is claimed by this node. |

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
