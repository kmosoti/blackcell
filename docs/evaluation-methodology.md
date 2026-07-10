---
node: evaluation-methodology
kind: evaluation-contract
edges:
  evaluates:
    - charter
    - architecture
---

# Evaluation Methodology

## Research questions

1. Does a conflict-preserving operational state estimate recover task-relevant state more
   accurately than latest-value or free-form summary baselines?
2. Does a structured ContextFrame improve valid decisions per context unit compared with raw
   chronological or latest-N evidence?
3. Does a typed symbolic gate reduce escaped policy violations without an unacceptable false
   rejection rate?
4. Do action-conditioned transition models eventually improve prediction and planning over
   persistence, symbolic, empirical, and LLM-only baselines?

## OperatorBench

OperatorBench is a deterministic repository-work simulator, not a benchmark of broad model
intelligence. Each scenario defines observations visible to the agent, hidden environment
state used only by graders, a task objective, available affordances, hard policies, expected
evidence, and acceptable outcomes.

Scenario families include:

- stale check results;
- conflicting source reports;
- missing required evidence;
- irrelevant distractors;
- dependency-blocked work;
- corrections arriving after initial observations;
- reordered and duplicated delivery;
- partial tool failures;
- unsafe write or readiness proposals;
- equivalent source-order permutations.

## Conditions

Every scenario is evaluated under matched model configuration and budget:

- raw chronological evidence;
- latest-N evidence;
- structured ContextFrame;
- structured ContextFrame with linked raw-evidence escalation.

Policy experiments compare prompt-only instructions, schema validation, developer-authored
Python policies, and optional solver-backed constraints when justified.

## Measures

| Concern | Measures |
| --- | --- |
| Outcome | task success, valid-action rate, partial credit |
| Evidence | required-evidence recall/precision, unsupported claim rate, conflict recall |
| State | slot accuracy, stale-state errors, unknown detection precision/recall |
| Policy | violation escape, false accept, false reject, repair success, approval rate |
| Context | characters/tokens, redundancy, selection and omission accuracy |
| System | latency, model calls, replay hash match, projection lag, orphan lineage records |
| Prediction | Brier score, log loss, reliability, impossible outcomes, rollout error |
| Planning | cumulative cost, regret to oracle, constraint violations, goal success |

## Trial protocol

- Begin with 20 to 30 scenarios and at least three trials for each stochastic model
  condition.
- Pair conditions by scenario, model, model configuration, and context budget.
- Preserve model identifier, timestamp, ContextFrame hash, response artifact, tool artifacts,
  and scorer version.
- Prefer deterministic environment and policy graders. Use blinded human rubrics for semantic
  utility and treat LLM judges as secondary evidence.
- Report Wilson intervals for proportions and paired bootstrap intervals for intervention
  differences when sample size permits.
- Split by chronology and scenario family. Do not use random row splits for transition-model
  evaluation.

## Promotion criteria

The pilot establishes effect sizes and variance before numerical thresholds are fixed.
Subsequent experiments preregister promotion criteria.

At minimum:

- kernel replay must be deterministic for all fixtures;
- no known hard-policy violation may escape the Phase 1 executor;
- a context intervention must improve success or materially reduce context at non-inferior
  success;
- a learned transition model must outperform simple baselines on held-out scenario families
  and improve downstream planning, not merely latent loss.

## Reproducibility

Public scenarios use synthetic data and recorded model fixtures. CI never requires personal
ChatGPT credentials. Live-model trials are separately labelled and stored as experiment
artifacts so deterministic tests remain independent of model availability.
