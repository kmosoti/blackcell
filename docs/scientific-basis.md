---
node: scientific-basis
kind: research-contract
edges:
  constrains:
    - architecture
    - evaluation-methodology
---

# Scientific Basis and Claim Discipline

Blackcell uses scientific ideas as sources of testable mechanisms. It does not use them as
synonyms for ordinary persistence, prompting, or telemetry.

## State estimation under partial observability

An `OperationalStateEstimate` is a versioned projection of claims supported by incomplete,
possibly stale, and possibly conflicting evidence. It is not a formal POMDP belief state.

Each claim records epistemic status, evidence references, source reliability class,
observation and effective times, freshness policy, conflict group, and derivation version.
Source reliability, evidence strength, freshness, and forecast probability are separate
quantities. Numeric forecast probabilities are introduced only where outcomes can be
observed and calibration can be measured.

Scientific extension: define bounded hidden-state hypotheses, observation and transition
models, normalized belief updates, and decision objectives for a specific domain.

## World models and predictive control

State estimation and transition prediction are separate interfaces:

```text
StateEstimator.reduce(events) -> OperationalStateEstimate
TransitionModel.predict(state, action, horizon) -> OutcomeForecast
Objective.score(outcome) -> utility
Planner.choose(candidates) -> action
```

Phase 1 records pre-state, candidate action, declared effects, observed post-state, outcome,
and residual. A developer-authored transition model or empirical frequency model is a valid
baseline. Blackcell earns the term `learned world model` only when a trained
action-conditioned predictor beats persistence, symbolic, empirical, and LLM forecast
baselines on held-out multi-step prediction and improves downstream planning.

The useful near-term control mechanism is receding-horizon execution: choose a short-horizon
action, execute one approved step, observe again, and replan. This bounds accumulated model
error and generates identifiable transition records.

## JEPA boundary

Deterministic hashes, feature sketches, frozen embeddings, or next-state labels are not a
JEPA. A JEPA claim requires context and target encoders, a predictor, a latent-space training
objective, anti-collapse machinery, and empirical comparison with simpler representation and
transition-learning baselines.

JEPA experiments remain under `experiments/` until Blackcell has enough domain-specific,
high-dimensional sequential data for the objective to be meaningful.

## Neural-symbolic boundary

Phase 1 is a hybrid control pipeline:

1. An LLM interprets a ContextFrame and proposes a typed action.
2. Developer-authored symbolic code evaluates permissions, invariants, preconditions, and
   approval requirements.
3. Only an allowed or explicitly approved proposal reaches an affordance executor.

This is described as **neural proposal with symbolic validation**. It becomes a
neuro-symbolic reasoning system only when symbolic inference or planning materially
participates in solving the task, or when neural and symbolic components are trained or
revised through a defined joint mechanism.

Start with pure Python predicates that return structured violations. Add SMT only when the
problem is genuinely combinatorial, numeric, temporal, or optimization-based. A solver proves
properties of the encoding, not the correctness of an LLM's interpretation.

## Context projection

Context projection is deterministic representation engineering in Phase 1. The projector
selects claims and raw evidence under explicit scope, freshness, sensitivity, relevance,
redundancy, and budget rules. It records what was selected, what was omitted, and why.

Structured keys and SQLite FTS5 are the initial retrieval baseline. Learned embeddings or
routers are later interventions and must be compared with that baseline at a matched context
budget.

## Provenance and causality

The event ledger and RunTrace establish execution lineage and derivation. They do not prove
causality. Causal claims require a causal model, intervention, or controlled experimental
design.

Blackcell follows the useful distinction in W3C PROV between entities, activities, agents,
usage, generation, and derivation without requiring RDF or OWL in the runtime.

## Promotion process

Each research intervention records:

```text
Hypothesis
Baseline
Intervention
Dataset or scenario family
Metrics and uncertainty
Ablations
Results
Limitations
Decision: promote, revise, or reject
```

Promotion is based on held-out task or planning performance, not merely internal loss,
architectural novelty, or fluent examples.
