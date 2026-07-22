---
node: charter
kind: charter
edges:
  informs:
    - architecture
    - scientific-basis
    - evaluation-methodology
---

# Blackcell Charter

## Canonical definition

**BlackCell is a CLI-first, project-scoped agentic framework with durable review and
verification.**

The alpha turns project intent and repository evidence into typed plans, bounded execution,
independent review, verified outcomes, and replayable records. The current runtime-v1 Repository
Operator and `DailyOperatorV2Workflow` are historical migration and replay evidence; they are not
the alpha execution path.

It converts immutable observations into domain-scoped operational state estimates,
builds inspectable context frames, accepts typed action proposals from models, validates
them against symbolic policies, executes approved affordances, and evaluates observed
outcomes.

Blackcell records action-conditioned transitions as the empirical substrate for future
predictive state representations and learned world models.

## Core thesis

For a fixed model and context budget, an explicit evidence state and deterministic context
projection should improve decision quality, provenance, and correction handling compared
with raw history. A typed symbolic gate should reduce invalid actions compared with
prompt-only constraints without an unacceptable false-rejection or latency cost.

Those are testable hypotheses. They are not assumed properties of the architecture.

The active product hypothesis is that explicit intent, repository evidence, a typed dependency
plan, separate prediction and verification, bounded execution, and independent review improve real
software work compared with an unconstrained model loop. Each capability is promoted through the
acceptance checks in `../alpha.plan.yaml`.

## Runtime responsibilities

Blackcell owns:

- immutable observation and outcome history;
- domain-scoped state estimation;
- provenance and conflict preservation;
- task-specific context projection;
- typed action proposals, policies, approvals, and affordances;
- execution lineage, replay, and evaluation;
- durable local orchestration, versioned service boundaries, telemetry, and recovery;
- prediction and outcome records that can support later transition models.

The model is a replaceable proposal mechanism. It is not the state store, policy engine,
executor, or source of truth.

The target project runtime additionally owns explicit intent and assumption records, evidence-bound
plans, plan and outcome verification, and replayable review findings when those contracts are
promoted. `scope.md` defines that target and its non-goals; it does not retroactively claim those
surfaces exist.

## Accepted Phase 1 product and research surface

The first vertical slice is the **Repository Operator**. It observes repository structure
and Git status, constructs an operational state estimate and SignalPacket, projects bounded
context, requests one typed proposal, evaluates policies, executes at most one bounded
read-only affordance, re-observes the environment, evaluates the outcome, and appends the
resulting evidence. Task and check adapters use the same repository-domain event contract.

The public `OperatorBench` scenarios exercise the same contracts with deterministic hidden
state, stale and conflicting evidence, distractors, corrections, unsafe proposals, and
partial failures. Its matched five-treatment report retains paired contexts, proposals, scores,
and uncertainty. The recorded fixture establishes the experiment contract, not a live-model
context or retrieval effect.

`DailyOperatorV2Workflow` is the historical runtime-v1 application path. It remains readable for
migration, replay, and extraction of useful contracts, but no new alpha client or daemon command
may invoke it.

## Accepted Phase 1 criteria

The WP09b product surface satisfies these acceptance criteria:

- One command completes the observe, project, propose, gate, act, re-observe, evaluate,
  and append loop.
- Every event occurrence has a unique identity; idempotency is represented separately.
- Historical replay verifies every referenced artifact and rebuilds recorded operational-state
  projections at their recorded cutoffs without invoking observers, models, or executors.
- The operational state estimate preserves conflicting claims and explicit unknowns.
- The ContextFrame is inspectable, content-addressed, budgeted, and explains selections
  and omissions.
- A developer-authored policy rejects at least one unsafe model proposal.
- The model cannot execute tools or mutate state outside Blackcell's affordance boundary.
- Human corrections append new evidence rather than rewriting history.
- OperatorBench validates raw, latest-N, structured, deterministic-term, and FTS5 context
  treatments plus evidence-visibility grading and paired uncertainty. The recorded WP23 result
  is explicitly non-inferential and leaves every product default unchanged.

## Runtime-v1 release-evidence completion

Phase 1 product acceptance is complete. Runtime-v1 is now evidence-complete as the broader local
platform program: the landed dependency join includes the canonical workflow and replay, deterministic
prediction, the promoted solver edge, durable role orchestration, the versioned API and process
boundary, rootless deployment, telemetry, and verified recovery through WP22.

WP25 now retains a complete six-probe runtime reliability baseline over the API, worker,
restart/fencing, quotas, recovery, and rootless-container deployment. The measured single-host
timings are harness evidence, not service SLOs or an optimization mandate. WP26 then retired the
prototype packages, independent stores, predecessor writers, obsolete CLI groups, and generated
OpenCode coordination artifacts after characterization and live-free replay evidence. WP27 now
binds the documentation, examples, locked Python-runtime SBOM, and reproducible verification
manifest. No runtime-v1 DAG node remains. This evidence is unpublished: it does not claim a built
or published package/image, tag, signature, provenance attestation, or vulnerability result. The
machine-readable dependency contract lives in `../blackcell.plan.yaml`; BCP-0034 remains the
canonical explanatory DAG and acceptance source.

## Claim gates

Use now:

- operational state estimate;
- structured state representation;
- context projection;
- neural proposal with symbolic validation;
- durable local DAG orchestration;
- versioned local runtime service;
- telemetry-derived signal packet;
- agentic systems runtime.
- verifier-aware durable role DAG;
- project-scoped product direction.

Reserve until measured mechanisms exist:

- POMDP belief state;
- calibrated uncertainty;
- predictive state representation;
- model-based planning;
- learned world model;
- JEPA architecture;
- neuro-symbolic reasoning system;
- causal understanding;
- control-theoretic stability;
- self-improving system.
- general project planning and implementation runtime;
- persistent user-intent model;
- sandboxed ephemeral-worktree executor;
- reward-hack-resistant reviewer subsystem;
- calibrated predictive risk model.

The promoted Clingo adapter establishes solver parity behind the existing symbolic-policy port;
it does not promote a neuro-symbolic-reasoning-system claim. The WP24 developer-declared-effect
baseline is likewise experiment-only and neither neural nor learned.

## Scope control

Every feature must improve at least one of state accuracy, context relevance, action safety,
execution lineage, evaluation quality, predictive value, or repeated practical utility.

Phase 1 excluded multi-agent orchestration and platform deployment work. Runtime-v1 has since
integrated a durable local role DAG, a versioned HTTP/process boundary, rootless Podman deployment,
OpenTelemetry export, and verified local recovery without retroactively expanding Phase 1.
The July 6 prototype has no remaining package, command, generated-agent, or persistence
compatibility surface; immutable version-one run history remains replay-readable only.

Distributed queues, Kubernetes, a visual workflow builder, custom neural training, and Rust
components remain out of scope. Graph or vector retrieval remains an experiment-only intervention.
WP23a promotes only the ephemeral FTS5 baseline, and the WP23 revise decision rejects a broader
quality or default claim until a sufficiently powered live comparison exists.

ADR 0009 establishes one foreground daemon as the authority for alpha state and scheduling. The
CLI, PyRatatui TUI, and Litestar web UI are clients of the same typed service. The active plan reuses
the current Python modular monolith, integrates Kernform through its pinned CLI contract, and
rejects a greenfield Rust/PyO3 rewrite, repository-local named-agent control plane, online
self-modification, and client-owned schedulers.

## Public positioning

Tagline: **Verifier-driven, project-scoped control runtime for evidence-grounded software work.**

Professional framing: **Observability / Platform Engineer building agentic AI systems.**
