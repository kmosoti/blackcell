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

**Blackcell is a local-first, event-sourced control runtime for evidence-grounded LLM
agents.**

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

## Runtime responsibilities

Blackcell owns:

- immutable observation and outcome history;
- domain-scoped state estimation;
- provenance and conflict preservation;
- task-specific context projection;
- typed action proposals, policies, approvals, and affordances;
- execution lineage, replay, and evaluation;
- prediction and outcome records that can support later transition models.

The model is a replaceable proposal mechanism. It is not the state store, policy engine,
executor, or source of truth.

## Phase 1 product and research surface

The first vertical slice is the **Repository Operator**. It observes repository structure
and Git status, constructs an operational state estimate and SignalPacket, projects bounded
context, requests one typed proposal, evaluates policies, executes at most one bounded
read-only affordance, re-observes the environment, evaluates the outcome, and appends the
resulting evidence. Task and check adapters use the same repository-domain event contract.

The public `OperatorBench` scenarios exercise the same contracts with deterministic hidden
state, stale and conflicting evidence, distractors, corrections, unsafe proposals, and
partial failures. A private Daily Operator can reuse the kernel after the public slice
establishes replay and evaluation integrity.

## Phase 1 acceptance criteria

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
- OperatorBench currently validates raw, latest-N, and structured-context rendering plus
  evidence-visibility grading using fixed scenarios. A model-dependent context-effect study
  remains a required Phase 3 experiment.

## Claim gates

Use now:

- operational state estimate;
- structured state representation;
- context projection;
- neural proposal with symbolic validation;
- telemetry-derived signal packet;
- agentic systems runtime.

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

## Scope control

Every feature must improve at least one of state accuracy, context relevance, action safety,
execution lineage, evaluation quality, predictive value, or repeated practical utility.

Phase 1 excludes multi-agent orchestration, vector and graph databases, distributed queues,
Kubernetes, a visual workflow builder, custom neural training, and Rust components. These
remain possible interventions after a measured need exists.

## Public positioning

Tagline: **Event-sourced control runtime for evidence-grounded LLM agents.**

Professional framing: **Observability / Platform Engineer building agentic AI systems.**
